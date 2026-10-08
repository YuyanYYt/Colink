/* Read-only, bounded database authorization checks before project code runs.
 * Credentials stay in memory and the child environment. No admin SQL is issued.
 */
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';

export function verifyMySqlGrants(text, expected) {
  if (!/^[A-Za-z0-9_][A-Za-z0-9_. -]{0,63}$/.test(expected))
    throw Error('Project database grants are too broad');
  const lines = text.trim().split('\n');
  const mode = lines.shift();
  if (!['0', '1'].includes(mode) || !lines.length)
    throw Error('Project database grants are too broad');
  for (const line of lines) {
    if (/WITH GRANT OPTION|PROXY|IDENTIFIED/i.test(line))
      throw Error('Project database grants are too broad');
    if (/^GRANT USAGE ON \*\.\* TO /i.test(line)) continue;
    const match = /^GRANT .+ ON (?:(?:TABLE|FUNCTION|PROCEDURE) )?`([^`]+)`\.(\*|`(?:[^`]|``)+`) TO .+$/i.exec(line);
    if (!match) throw Error('Project database grants are too broad');
    let database = match[1];
    // Only database-level grants use SQL wildcards when partial_revokes=OFF.
    // Table/routine qualifiers and partial_revokes=ON use literal names.
    if (mode === '0' && match[2] === '*') {
      let literal = '';
      for (let index = 0; index < database.length; index++) {
        const char = database[index];
        if (char === '\\') {
          const next = database[++index];
          if (!['_', '%', '\\'].includes(next))
            throw Error('Project database grants are too broad');
          literal += next;
        } else if (char === '_' || char === '%') {
          throw Error('Project database grants are too broad');
        } else literal += char;
      }
      database = literal;
    }
    if (database !== expected) throw Error('Project database grants are too broad');
  }
  return { role_check: 'passed' };
}

export function mysqlConnectionArgs(config, env) {
  return ['--no-defaults', '--no-login-paths', '--protocol=TCP',
    '--host=' + env.MYSQL_HOST, '--port=' + env.MYSQL_TCP_PORT,
    '--ssl-mode=' + (config.databaseTLS ? 'VERIFY_IDENTITY' : 'DISABLED'),
    ...(!config.databaseTLS ? ['--get-server-public-key'] : []),
    '--local-infile=0', '--user=' + config.databaseUser, '--database=' + env.COLINK_DATABASE_NAME];
}

export function mysqlProjectArgs(config, env, args) {
  const switches = new Set(['--batch', '--raw', '--skip-column-names', '--silent',
    '--show-warnings', '--force', '--verbose', '-B', '-N', '-s', '-f', '-v']);
  for (let index = 0; index < args.length; index++) {
    const value = args[index];
    if (switches.has(value) || /^-v{1,3}$/.test(value)) continue;
    if (value === '-e' || value === '--execute') {
      if (typeof args[++index] !== 'string') throw Error('Project SQL required');
      continue;
    }
    if (value.startsWith('--execute=') || /^-e.+/.test(value)) continue;
    if (/^--connect-timeout=[1-9][0-9]?$/.test(value)) continue;
    // Reject connection overrides and abbreviated MySQL options alike.
    throw Error('Use the configured project database connection');
  }
  return [config.databaseClient, ...mysqlConnectionArgs(config, env), ...args];
}

function probe(profile, argv, env, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/sandbox-exec', ['-p', profile, ...argv], {
      env, cwd, stdio: ['ignore', 'pipe', 'pipe'], detached: false,
    });
    let size = 0, text = '', failed = false;
    const timer = setTimeout(() => { failed = true; child.kill('SIGKILL'); }, 10000);
    child.stdout.on('data', part => {
      size += part.length;
      if (size > 16384) { failed = true; child.kill('SIGKILL'); }
      else text += part.toString('utf8');
    });
    // Client errors can contain connection details. They never enter job logs.
    child.stderr.on('data', part => {
      size += part.length;
      if (size > 16384) { failed = true; child.kill('SIGKILL'); }
    });
    child.once('error', () => { clearTimeout(timer); reject(Error('Database client unavailable')); });
    child.once('close', code => {
      clearTimeout(timer);
      if (failed) reject(Error('Database probe budget'));
      else resolve({ code, text });
    });
  });
}

export async function verifyDatabase(profile, config, env) {
  const kind = env.COLINK_DATABASE_KIND;
  if (!kind) return undefined;
  const expected = env.COLINK_DATABASE_NAME;
  let argv, passwordKey, verify;
  if (kind === 'postgresql' || kind === 'pgvector') {
    const sql = `WITH RECURSIVE member(oid) AS (
      SELECT oid FROM pg_roles WHERE rolname=current_user
      UNION SELECT m.roleid FROM pg_auth_members m JOIN member r ON m.member=r.oid
    ) SELECT json_build_object('database',current_database(), 'user',current_user,
      'instance_identity',(SELECT oid::text FROM pg_database WHERE datname=current_database()),
      'unsafe_role',EXISTS(SELECT 1 FROM pg_roles WHERE oid IN (SELECT oid FROM member)
        AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
          OR left(rolname,3)='pg_')),
      'other_database_owner',EXISTS(SELECT 1 FROM pg_database WHERE datname<>current_database()
        AND datdba IN (SELECT oid FROM member)),
      'vector_extension',EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector'))::text;`;
    argv = [config.databaseClient, '--no-psqlrc', '--no-password', '--tuples-only',
      '--no-align', '--set', 'ON_ERROR_STOP=1', '--command', sql];
    passwordKey = 'PGPASSWORD';
    verify = text => {
      const result = JSON.parse(text.trim());
      if (result.database !== expected || result.user !== config.databaseUser)
        throw Error('Database target changed');
      if (!config.databaseAction && config.databaseInstanceIdentity &&
          result.instance_identity !== config.databaseInstanceIdentity)
        throw Error('Database instance changed');
      if (!config.databaseApprovedAccount && (result.unsafe_role || result.other_database_owner))
        throw Error('Project database role is too broad');
      return { vector_extension: result.vector_extension,
        role_check: config.databaseApprovedAccount ? 'approved_account' : 'passed' };
    };
  } else if (kind === 'mysql') {
    argv = [config.databaseClient, ...mysqlConnectionArgs(config, env), '--batch', '--raw',
      '--skip-column-names', config.databaseApprovedAccount
        ? '--execute=SELECT JSON_OBJECT(\'database\', DATABASE(), \'user\', SUBSTRING_INDEX(CURRENT_USER(),\'@\',1));'
        : '--execute=SELECT @@GLOBAL.partial_revokes; SHOW GRANTS FOR CURRENT_USER();'];
    passwordKey = 'MYSQL_PWD';
    verify = text => {
      if (!config.databaseApprovedAccount) return verifyMySqlGrants(text, expected);
      const result = JSON.parse(text.trim());
      if (result.database !== expected || result.user !== config.databaseUser)
        throw Error('Database target changed');
      return { role_check: 'approved_account' };
    };
  } else {
    // A global vector-server API key cannot isolate another project's data.
    throw Error('Dedicated vector instance or scoped credential required');
  }
  if (typeof config.databaseClient !== 'string' || !config.databaseClient.startsWith('/'))
    throw Error('Project database client unavailable');
  const positive = await probe(profile, argv, env, config.cwd);
  if (positive.code !== 0) throw Error('Project database authentication failed');
  verify(positive.text);
  const invalid = { ...env, [passwordKey]: 'colink-negative-' + randomBytes(24).toString('hex') };
  const negative = await probe(profile, argv, invalid, config.cwd);
  if (negative.code === 0 && !config.databaseApprovedAccount && !config.databaseTargetEnforced)
    throw Error('Database endpoint does not enforce password authentication');
  const again = await probe(profile, argv, env, config.cwd);
  if (again.code !== 0) throw Error('Database authentication could not be confirmed');
  const result = verify(again.text);
  return { kind, authenticated: true, password_enforced: negative.code !== 0, ...result,
    database_action: config.databaseAction || null,
    target_enforced: config.databaseTargetEnforced === true,
    migrations: 'not_verified', crud: 'not_verified',
    data_scope: config.databaseApprovedAccount
      ? 'approved target; native account privileges remain authoritative'
      : 'project role; server database grants remain authoritative' };
}
