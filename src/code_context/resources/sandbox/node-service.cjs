/* CoLink's registered Unix endpoint adapter for Node HTTP/development servers.
 * Loaded inside the native sandbox. TCP bind remains denied by the kernel.
 */
'use strict';
const net = require('node:net');
const endpoints = JSON.parse(process.env.COLINK_SOCKET_ENDPOINTS || '{}');
const originalListen = net.Server.prototype.listen;
const originalAddress = net.Server.prototype.address;
const attached = new WeakMap();
net.Server.prototype.listen = function (...args) {
  const first = args[0];
  const port = typeof first === 'object' && first !== null ? first.port : first;
  const endpoint = endpoints[String(port)];
  if (!endpoint) return originalListen.apply(this, args);
  const callback = typeof args.at(-1) === 'function' ? args.at(-1) : undefined;
  const backlog = typeof first === 'object' ? first.backlog : args.find((value, index) => index > 0 && Number.isInteger(value));
  attached.set(this, Number(port));
  return originalListen.call(this, { path: endpoint, backlog }, callback);
};
net.Server.prototype.address = function () {
  const address = originalAddress.call(this);
  const port = attached.get(this);
  return port && address ? { address: '127.0.0.1', family: 'IPv4', port } : address;
};
