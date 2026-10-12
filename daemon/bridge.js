/* Remote Bridge daemon: reads the TV remote's button presses and streams them
 * to the Kodi add-on (service.webostv) over TCP.
 *
 * Runs as root under its own systemd unit (see 97-kodiremote). It only reads
 * the input devices, so the TV keeps handling every button as usual; Kodi
 * decides what to do with them, and ignores them unless Kodi is on screen.
 *
 * Plain ES5 on purpose: webOS 5 ships an old Node.
 *
 * Protocol: one JSON object per line.
 *   -> {"hello": {"token", "version", "node", "arch", "devices": [names]}}
 *   -> {"key": code, "value": 0 up | 1 down | 2 repeat, "dev": index}
 *   -> {"wheel": steps, "dev": index}
 *   <- {"ok": true} once the token is accepted; anything else closes.
 */

var fs = require('fs');
var net = require('net');
var spawn = require('child_process').spawn;

var VERSION = '0.1.1';
// Overridable for tests off the TV.
var CONFIG = process.env.KODIREMOTE_CONFIG || '/var/lib/webosbrew/kodiremote.json';
var LOG = process.env.KODIREMOTE_LOG || '/home/root/kodiremote.log';
var DEVICES = process.env.KODIREMOTE_DEVICES || '/proc/bus/input/devices';
var INPUT_DIR = process.env.KODIREMOTE_INPUT_DIR || '/dev/input';
var DEFAULT_DEVICES = '^LGE (M-RCU|RCU|TONE|W-RCU|Simple)';
var RETRY_MS = 5000;
var RESCAN_MS = 30000;
// The same press can arrive on more than one remote device; drop the copies.
var DUP_MS = 25;

var EV_KEY = 1;
var EV_REL = 2;
var REL_WHEEL = 8;
// struct input_event: a timeval (two longs), then u16 type, u16 code, s32 value.
var EVENT_SIZE = (process.arch === 'arm64' || process.arch === 'x64') ? 24 : 16;

var config = null;
var socket = null;
var accepted = false;
var devices = {};      // path -> {name, index, stream}
var deviceCount = 0;
var lastEvent = { key: '', time: 0, dev: -1 };

function log(text) {
  var line = new Date().toISOString() + ' ' + text + '\n';
  try {
    if (fs.existsSync(LOG) && fs.statSync(LOG).size > 100000) {
      var tail = fs.readFileSync(LOG).slice(-50000);
      fs.writeFileSync(LOG, tail);
    }
    fs.appendFileSync(LOG, line);
  } catch (e) { /* logging is best effort */ }
}

function readConfig() {
  try {
    var c = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
    if (c && c.host && c.port && c.token) return c;
  } catch (e) { /* not configured yet */ }
  return null;
}

/* ---- input devices ---- */

function listDevices(pattern) {
  var text;
  try { text = fs.readFileSync(DEVICES, 'utf8'); } catch (e) { return []; }
  var re = new RegExp(pattern);
  var found = [];
  text.split(/\n\s*\n/).forEach(function (block) {
    var name = (block.match(/^N: Name="(.*)"$/m) || [])[1];
    var handlers = (block.match(/^H: Handlers=(.*)$/m) || [])[1] || '';
    var event = (handlers.match(/\bevent\d+\b/) || [])[0];
    if (name && event && re.test(name)) found.push({ name: name, path: INPUT_DIR + '/' + event });
  });
  return found;
}

function send(obj) {
  if (socket && accepted) socket.write(JSON.stringify(obj) + '\n');
}

function onEvent(dev, type, code, value) {
  var msg;
  if (type === EV_KEY) {
    msg = { key: code, value: value, dev: dev.index };
  } else if (type === EV_REL && code === REL_WHEEL && value !== 0) {
    msg = { wheel: value, dev: dev.index };
  } else {
    return;
  }
  var id = (msg.key !== undefined ? 'k' + code + ':' + value : 'w' + value);
  var now = Date.now();
  if (id === lastEvent.key && dev.index !== lastEvent.dev && now - lastEvent.time < DUP_MS) return;
  lastEvent = { key: id, time: now, dev: dev.index };
  send(msg);
}

/* Each device is read by a `cat` child: a blocking read on an idle device
 * would otherwise hold one of libuv's 4 threadpool threads for good, and with
 * more devices than threads the remote's own device stops being read. Pipes
 * are polled on the event loop instead. */
function openDevice(info) {
  var dev = { name: info.name, index: deviceCount++, proc: null };
  var pending = Buffer.alloc ? Buffer.alloc(0) : new Buffer(0);
  var proc = spawn('cat', [info.path], { stdio: ['ignore', 'pipe', 'ignore'] });
  dev.proc = proc;
  devices[info.path] = dev;
  proc.stdout.on('data', function (chunk) {
    var buf = Buffer.concat([pending, chunk]);
    var off = 0;
    var base = EVENT_SIZE - 8;
    for (; off + EVENT_SIZE <= buf.length; off += EVENT_SIZE) {
      onEvent(dev, buf.readUInt16LE(off + base), buf.readUInt16LE(off + base + 2),
              buf.readInt32LE(off + base + 4));
    }
    pending = buf.slice(off);
  });
  proc.on('error', function (err) {
    log('read ' + info.path + ' (' + info.name + ') failed: ' + err.message);
  });
  proc.on('exit', function (code) {
    if (devices[info.path] === dev) delete devices[info.path];
    log('stopped reading ' + info.path + ' (' + info.name + '), exit ' + code);
  });
  log('reading ' + info.path + ' (' + info.name + ')');
}

function scanDevices() {
  var pattern = (config && config.devices) || DEFAULT_DEVICES;
  listDevices(pattern).forEach(function (info) {
    if (!devices[info.path]) openDevice(info);
  });
}

function deviceNames() {
  var names = [];
  Object.keys(devices).forEach(function (p) { names[devices[p].index] = devices[p].name; });
  return names;
}

/* ---- connection to Kodi ---- */

function connect() {
  config = readConfig();
  if (!config) {
    setTimeout(connect, RETRY_MS);
    return;
  }
  var sock = net.connect(config.port, config.host);
  var buffered = '';
  socket = sock;
  accepted = false;
  sock.setNoDelay(true);
  sock.setKeepAlive(true, 15000);
  sock.on('connect', function () {
    sock.write(JSON.stringify({ hello: {
      token: config.token, version: VERSION, node: process.version, arch: process.arch,
      devices: deviceNames()
    } }) + '\n');
  });
  sock.on('data', function (data) {
    buffered += data.toString();
    var lines = buffered.split('\n');
    buffered = lines.pop();
    lines.forEach(function (line) {
      var msg;
      try { msg = JSON.parse(line); } catch (e) { return; }
      if (msg.ok && !accepted) {
        accepted = true;
        log('connected to Kodi at ' + config.host + ':' + config.port);
      }
    });
  });
  sock.on('error', function () { /* close follows */ });
  sock.on('close', function () {
    if (accepted) log('Kodi connection closed');
    accepted = false;
    if (socket === sock) socket = null;
    setTimeout(connect, RETRY_MS);
  });
}

/* An app update replaces this file but leaves the running process alone.
 * Exit when it changes; systemd (Restart=always) starts the new version. */
var selfStamp = stamp(__filename);
function stamp(path) {
  try { var st = fs.statSync(path); return st.size + ':' + st.mtime.getTime(); } catch (e) { return ''; }
}
function checkUpdated() {
  var now = stamp(__filename);
  if (now && now !== selfStamp) {
    log('app updated, restarting');
    process.exit(0);
  }
}

log('Remote Bridge ' + VERSION + ' started (node ' + process.version + ', ' + process.arch + ')');
config = readConfig();
scanDevices();
setInterval(function () { checkUpdated(); scanDevices(); }, RESCAN_MS);
connect();
