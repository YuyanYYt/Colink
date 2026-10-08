/* CoLink native sandbox adapter. SRT 0.0.78, Apache-2.0, Anthropic PBC.
 * Policy tightening and argv-only child supervision by CoLink, 2026.
 * Never evaluates a command or configuration outside the native sandbox.
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { verifyDatabase, mysqlProjectArgs } from './database-probe.mjs';
import { SandboxManager } from '@anthropic-ai/sandbox-runtime';
import { wrapCommandWithSandboxMacOS } from './node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/macos-sandbox-utils.js';
import { getJavaProxyAgentJarPathAsync, buildJavaToolOptions } from './node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/java-proxy-agent.js';
let stage = 'configuration';

async function databasePayload(required) {
  if (!required) return { env: {}, redactions: [] };
  let size = 0; const parts = [];
  for await (const part of process.stdin) {
    size += part.length;
    if (size > 65536) throw Error('Database payload limit');
    parts.push(part);
  }
  return validateDatabasePayload(JSON.parse(Buffer.concat(parts).toString('utf8')));
}

export function validateDatabasePayload(payload) {
  const keys = new Set(['COLINK_DATABASE_NAME', 'COLINK_DATABASE_KIND', 'DATABASE_URL',
    'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGSERVICEFILE',
    'COLINK_DB_PASSWORD', 'SPRING_DATASOURCE_URL', 'SPRING_DATASOURCE_USERNAME',
    'SPRING_DATASOURCE_PASSWORD',
    'MYSQL_HOST', 'MYSQL_TCP_PORT', 'MYSQL_PWD', 'QDRANT_URL', 'QDRANT_API_KEY']);
  if (!payload || typeof payload !== 'object' || !payload.env ||
      typeof payload.env !== 'object' || Array.isArray(payload.env) ||
      Object.entries(payload.env).some(([key, value]) => !keys.has(key) || typeof value !== 'string' || value.includes('\0')) ||
      !Array.isArray(payload.redactions) || payload.redactions.some(value => typeof value !== 'string')) throw Error('Invalid database payload');
  return payload;
}

function javaOption(value) {
  if (value.includes('"') || value.includes('\n')) throw Error('Unsupported JVM temporary path');
  return /\s/.test(value) ? `"${value}"` : value;
}

// Decode only the pinned library's quote() serialization, with no expansions.
export function decodeQuoted(value) {
  const words = []; let word = '', mode = '', active = false;
  for (const char of value) {
    if (mode) { if (char === mode) mode = ''; else word += char; }
    else if (char === "'" || char === '"') { mode = char; active = true; }
    else if (char === ' ') { if (active) { words.push(word); word = ''; active = false; } }
    else { if (!/[A-Za-z0-9_./:=@+,-]/.test(char)) throw Error('Unsupported SRT serialization'); word += char; active = true; }
  }
  if (mode) throw Error('Invalid SRT serialization');
  if (active) words.push(word);
  return words;
}

export function tighten(profile, config, proxyPorts) {
  const start = profile.indexOf('; Network\n');
  const end = profile.indexOf('; File read\n', start);
  if (start < 0 || end < 0 || profile.includes('(allow network*)')) throw Error('Invalid restrictive profile');
  const network = ['; CoLink exact registered TCP endpoints'];
  for (const port of new Set([...proxyPorts, ...config.connectPorts]))
    network.push(`(allow network-outbound (remote ip "localhost:${port}"))`);
  // Seatbelt's localhost bind includes wildcard addresses. Bind no project TCP
  // sockets; the trusted coordinator owns exact loopback TCP listeners.
  for (const endpoint of Object.values(config.socketEndpoints ?? {})) {
    network.push(`(allow network-bind (local unix-socket (literal ${JSON.stringify(endpoint)})))`);
    network.push(`(allow network-inbound (local unix-socket (literal ${JSON.stringify(endpoint)})))`);
  }
  profile = profile.slice(0, start) + network.join('\n') + '\n' + profile.slice(end);
  // Remove SRT allowances which are unnecessary for noninteractive development.
  profile = profile.split('\n').filter(line => ![
    'com.apple.audio.', 'com.apple.distributed_notifications', 'com.apple.Font',
    'com.apple.fonts', 'com.apple.PowerManagement', 'com.apple.securityd',
    'com.apple.SecurityServer', 'com.apple.coreservices.', 'com.apple.lsd.',
    'RootDomainUserClient', 'IOSurface', '(allow user-preference-read)',
    '(allow distributed-notification-post)', '(allow mach-priv-task-port',
    '(allow file-ioctl (literal "/dev/tty"))',
  ].some(part => line.includes(part))).join('\n');
  // Remove the now empty iokit-open rule; no user clients are granted.
  profile = profile.replace(/\(allow iokit-open\s*\)/g, '');
  profile += '\n; CoLink controller and read-only FD protections\n';
  // Adapted from openai/codex d27764b82, sandboxing/seatbelt.rs (Apache-2.0).
  profile += '(deny system-fcntl (fcntl-command 80 110))\n';
  profile += '(deny mach-lookup (xpc-service-name-prefix ""))\n';
  // SRT canonicalizes allowRead paths. dyld must also read the symlink
  // metadata for the exact original installation chain, without gaining data
  // access to other files in those directories.
  for (const alias of config.readMetadataPaths ?? []) {
    profile += `(allow file-read-metadata (literal ${JSON.stringify(alias)}))\n`;
  }
  for (const path of config.protectedPaths) {
    profile += `(deny file-read* file-write* (subpath ${JSON.stringify(path)}))\n`;
    let parent = path;
    while (parent !== '/') {
      profile += `(deny file-write-unlink (literal ${JSON.stringify(parent)}))\n`;
      parent = parent.slice(0, parent.lastIndexOf('/')) || '/';
    }
  }
  return profile;
}

async function main() {
  if (process.platform !== 'darwin' || process.argv.length !== 3) throw Error('Native macOS sandbox required');
  const info = fs.lstatSync(process.argv[2]);
  if (!info.isFile() || info.nlink !== 1 || (info.mode & 0o077)) throw Error('Private job configuration required');
  const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  for (const key of ['bindPorts', 'connectPorts']) {
    if (!Array.isArray(config[key]) || config[key].some(p => !Number.isInteger(p) || p < 1024 || p > 65535)) throw Error('Invalid registered ports');
  }
  if (!Array.isArray(config.argv) || !config.argv.length || !config.argv[0].startsWith('/')) throw Error('Explicit executable required');
  const database = await databasePayload(config.databasePayload);
  process.chdir(config.cwd);
  // The helper receives a clean environment from CoLink; credentials/profiles
  // are never read. All temporary material belongs to this job's bounded disk.
  stage = 'proxy_initialization';
  await SandboxManager.initialize({
    network: { allowedDomains: config.domains, deniedDomains: [], allowLocalBinding: false },
    filesystem: { denyRead: ['/'], allowRead: config.readRoots, allowWrite: config.writeRoots, denyWrite: config.protectedPaths },
    allowPty: false, allowGitConfig: false, allowAppleEvents: false,
  }, undefined, false);
  await SandboxManager.waitForNetworkInitialization();
  stage = 'policy_generation';
  const wrapped = wrapCommandWithSandboxMacOS({
    command: 'unused', needsNetworkRestriction: true,
    httpProxyPort: SandboxManager.getProxyPort(), socksProxyPort: SandboxManager.getSocksProxyPort(),
    proxyAuthToken: SandboxManager.getProxyAuthToken(),
    readConfig: { denyOnly: ['/'], allowWithinDeny: config.readRoots },
    writeConfig: { allowOnly: config.writeRoots, denyWithinAllow: config.protectedPaths },
    allowLocalBinding: false, allowPty: false, binShell: '/bin/sh',
  });
  const argv = decodeQuoted(wrapped);
  const sandboxAt = argv.indexOf('/usr/bin/sandbox-exec');
  if (sandboxAt < 1 || argv[sandboxAt + 1] !== '-p' || argv[0] !== 'env') throw Error('SRT interface changed');
  const childEnv = { ...process.env };
  Object.assign(childEnv, database.env);
  for (const assignment of argv.slice(1, sandboxAt)) {
    const equals = assignment.indexOf('=');
    if (equals < 1) throw Error('Unsupported SRT environment');
    childEnv[assignment.slice(0, equals)] = assignment.slice(equals + 1);
  }
  // Override SRT's default /tmp/claude and avoid DNS inside the restricted child.
  childEnv.TMPDIR = config.childTmp;
  childEnv.TMP = config.childTmp;
  childEnv.TEMP = config.childTmp;
  for (const key of ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'GRPC_PROXY', 'grpc_proxy', 'FTP_PROXY', 'ftp_proxy']) {
    if (!childEnv[key]) continue;
    const address = new URL(childEnv[key]);
    if (address.hostname !== 'localhost' && address.hostname !== '127.0.0.1') throw Error('Unexpected proxy address');
    address.hostname = '127.0.0.1';
    childEnv[key] = address.toString();
  }
  // JVM dual-stack IPv4-mapped loopback does not match Seatbelt localhost.
  // Explicit IPv4 also prevents wildcard listener workarounds.
  const jar = await getJavaProxyAgentJarPathAsync();
  if (config.domains.length && !jar) throw Error('JVM proxy adapter missing');
  childEnv.JAVA_TOOL_OPTIONS = buildJavaToolOptions({ agentJarPath: config.domains.length ? jar : undefined,
    flags: ['-Djava.net.preferIPv4Stack=true', '-XX:+PerfDisableSharedMem',
      javaOption(`-Djava.io.tmpdir=${config.childTmp}`), javaOption(`-Djansi.tmpdir=${config.childTmp}`)] });
  let mavenSettings;
  if (path.basename(config.argv[0]) === 'mvn' && config.domains.length) {
    // Maven Resolver's Apache transport does not consistently use JDK's proxy
    // selector. A short-lived settings file supplies the same restricted proxy.
    mavenSettings = path.join(config.childTmp, 'colink-maven-settings.xml');
    const token = SandboxManager.getProxyAuthToken();
    const escape = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    const username = decodeURIComponent(new URL(childEnv.HTTPS_PROXY).username);
    fs.writeFileSync(mavenSettings, `<settings><proxies><proxy><id>colink</id><active>true</active><protocol>http</protocol><host>127.0.0.1</host><port>${SandboxManager.getProxyPort()}</port><username>${escape(username)}</username><password>${escape(token)}</password><nonProxyHosts>localhost|127.0.0.1</nonProxyHosts></proxy></proxies></settings>`, { mode: 0o600, flag: 'wx' });
    config.argv.splice(1, 0, '--settings', mavenSettings);
  }
  const profile = tighten(argv[sandboxAt + 2], config, [SandboxManager.getProxyPort(), SandboxManager.getSocksProxyPort()].filter(Boolean));
  stage = 'database_authorization';
  const proof = await verifyDatabase(profile, config, childEnv);
  if (childEnv.COLINK_DATABASE_KIND === 'mysql' && config.argv[0] === config.databaseClient)
    config.argv = mysqlProjectArgs(config, childEnv, config.argv.slice(1));
  if (proof) fs.writeFileSync(process.argv[2] + '.database-proof', JSON.stringify(proof), { mode: 0o600, flag: 'wx' });
  stage = 'native_launch';
  const child = spawn('/usr/bin/sandbox-exec', ['-p', profile, ...config.argv], {
    cwd: config.cwd, env: childEnv, stdio: ['ignore', 'pipe', 'pipe'], detached: false,
  });
  // Filter short-lived proxy passwords before they enter durable output.
  const tokens = [SandboxManager.getProxyAuthToken(), ...database.redactions].filter(Boolean);
  for (const [input, output] of [[child.stdout, process.stdout], [child.stderr, process.stderr]]) {
    input.setEncoding('utf8'); let pending = '';
    input.on('data', text => { pending += text; for (const token of tokens) pending = pending.replaceAll(token, '[project credential]'); let keep = 0;
      for (const token of tokens) for (let n = 1; n < token.length && n <= pending.length; n++) if (token.startsWith(pending.slice(-n))) keep = Math.max(keep, n);
      if (pending.length > keep) { const safe = pending.slice(0, pending.length - keep); pending = pending.slice(pending.length - keep); output.write(safe); }
    });
    input.on('end', () => { for (const token of tokens) pending = pending.replaceAll(token, '[project credential]'); output.write(pending); });
  }
  let requestedSignal;
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => { requestedSignal = signal; child.kill(signal); });
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', (code, signal) => resolve(code ?? (128 + (os.constants.signals[signal] ?? 1)))); });
  stage = 'proxy_cleanup';
  await SandboxManager.reset();
  if (mavenSettings) fs.unlinkSync(mavenSettings);
  if (config.proxyTmp) { try { fs.rmdirSync(config.proxyTmp); } catch {} }
  process.exitCode = requestedSignal && code === 0 ? 128 + os.constants.signals[requestedSignal] : code;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch(async error => { process.stderr.write(`COLINK_SANDBOX_FAILED: ${stage} (${error?.name === 'TypeError' ? 'invalid configuration' : 'native dependency or policy unavailable'})\n`); await SandboxManager.reset().catch(() => {}); process.exitCode = 125; });
}
