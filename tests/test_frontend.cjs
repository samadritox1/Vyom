const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
function functionSource(name, nextName) {
  const start = html.indexOf(`    function ${name}(`);
  const end = html.indexOf(`    function ${nextName}(`, start);
  assert.ok(start >= 0 && end > start);
  return html.slice(start, end);
}

test('all inline browser scripts and the service worker parse', () => {
  let count = 0;
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc\s*=/.test(match[1])) continue;
    new vm.Script(match[2]);
    count++;
  }
  assert.ok(count >= 2);
  new vm.Script(fs.readFileSync(path.join(root, 'sw.js'), 'utf8'));
});

test('WebSocket sends a fresh session token as its first frame, without putting it in the URL', async () => {
  const created = [];
  class WebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; this.frames = []; created.push(this); }
    send(frame) { this.frames.push(JSON.parse(frame)); }
    close() { this.readyState = 3; }
  }
  let tokenRequests = 0;
  const context = { WebSocket, currentClerkUser: {id: 'user_a'}, setTimeout() {},
    window: { location: {protocol: 'https:', host: 'vyom.example'},
      Clerk: { session: { async getToken() { tokenRequests++; return 'fresh-test-token'; } } } } };
  vm.createContext(context);
  vm.runInContext(functionSource('initWebSocket', 'displayLiveAlert'), context);
  context.initWebSocket('user_a');
  await created[0].onopen();
  assert.equal(created[0].url, 'wss://vyom.example/ws/user_a');
  assert.deepEqual(created[0].frames, [{token: 'fresh-test-token'}]);
  assert.equal(tokenRequests, 1);
});

test('a socket created for another account cannot obtain or send a token', async () => {
  const created = [];
  class WebSocket {
    static OPEN = 1;
    constructor() { this.readyState = 1; created.push(this); }
    send() { assert.fail('foreign socket must not send'); }
    close() { this.readyState = 3; }
  }
  const context = { WebSocket, currentClerkUser: {id: 'user_b'}, setTimeout() {},
    window: {location: {protocol: 'https:', host: 'vyom.example'},
      Clerk: {session: {getToken() {assert.fail('foreign socket must not request token');}}}} };
  vm.createContext(context);
  vm.runInContext(functionSource('initWebSocket', 'displayLiveAlert'), context);
  context.initWebSocket('user_a');
  await created[0].onopen();
  assert.equal(created[0].readyState, 3);
});

test('live authority text cannot become executable HTML', () => {
  function element() {
    return {style: {}, children: [], append(...values) {this.children.push(...values);},
      appendChild(value) {this.children.push(value);},
      set innerHTML(_) {assert.fail('alert text must not use innerHTML');}};
  }
  const chat = element(), greeting = element();
  const context = {document: {createElement: element,
    getElementById(id) {return id === 'chat-area' ? chat : greeting;}}};
  vm.createContext(context);
  vm.runInContext(functionSource('displayLiveAlert', 'applyProfileToUI'), context);
  context.displayLiveAlert('<img onerror=alert(1)>', '<script>alert(1)</script>');
  const contents = chat.children[0].children[1].children;
  assert.equal(contents[0].textContent, 'VYOM Alert: <img onerror=alert(1)>');
  assert.equal(contents[1].textContent, '<script>alert(1)</script>');
});

test('notification clicks stay on the application origin', async () => {
  const listeners = {}, opened = [];
  const context = {URL, self: {location: {origin: 'https://vyom.example'},
    addEventListener(name, handler) {listeners[name] = handler;}},
    clients: {openWindow(url) {opened.push(url); return Promise.resolve();}}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'sw.js'), 'utf8'), context);
  for (const [requested, expected] of [
    ['https://attacker.invalid/phishing', 'https://vyom.example/'],
    ['//attacker.invalid/phishing', 'https://vyom.example/'],
    ['javascript:alert(1)', 'https://vyom.example/'],
    ['/alerts', 'https://vyom.example/alerts'],
  ]) {
    let pending;
    listeners.notificationclick({notification: {close() {}, data: {url: requested}},
      waitUntil(value) {pending = value;}});
    await pending;
    assert.equal(opened.at(-1), expected);
  }
});
