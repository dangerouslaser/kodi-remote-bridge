/* Remote Bridge for Kodi: frontend.
 *
 * Kodi (service.webostv) launches this app with parameters to set it up or
 * remove it, so nobody has to open a shell on the TV:
 *   {"kodiHost", "kodiPort", "token", "returnTo"}  set up, then switch back
 *   {"action": "remove", "returnTo"}               remove, then switch back
 *   {"action": "update", "ipkUrl", "ipkHash", "returnTo"}
 *                                                  install a newer version (the one
 *                                                  bundled with Kodi's add-on), then
 *                                                  switch back
 * Opened from the launcher it shows the bridge's status.
 *
 * Privileged work goes through the Homebrew Channel root shell
 * (luna://org.webosbrew.hbchannel.service/exec), the same pattern as WoL Keeper.
 */

var APP_ID = 'com.github.dangerouslaser.kodiremote';
var ASSETS = '/media/developer/apps/usr/palm/applications/' + APP_ID + '/assets';
var HOOK = '/var/lib/webosbrew/init.d/97-kodiremote';
var CONFIG = '/var/lib/webosbrew/kodiremote.json';
var UNIT = 'kodiremote.service';
var LOGFILE = '/home/root/kodiremote.log';
var RETURN_DELAY = 1500;

/* --- plumbing --- */

function luna(uri, params, cb) {
  var bridge = new PalmServiceBridge();
  bridge.onservicecallback = function (msg) {
    var r;
    try { r = JSON.parse(msg); } catch (e) { r = { returnValue: false, errorText: String(msg) }; }
    if (cb) cb(r);
  };
  bridge.call(uri, JSON.stringify(params || {}));
}

function rootExec(command, cb) {
  luna('luna://org.webosbrew.hbchannel.service/exec', { command: command }, cb);
}

function sq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function output(r) {
  return ((r && r.stdoutString) || '') + ((r && r.stderrString) || '');
}

/* --- ui --- */

function setDot(id, cls, text) {
  document.getElementById('dot-' + id).className = 'dot' + (cls ? ' ' + cls : '');
  document.getElementById('v-' + id).textContent = text;
}

function log(text) {
  document.getElementById('log').textContent = text;
}

function headline(text) {
  document.getElementById('headline').textContent = text;
}

/* Hand the screen back to Kodi's input, then get out of the way. */
function returnTo(appId) {
  if (!appId) return;
  setTimeout(function () {
    luna('luna://com.webos.applicationManager/launch', { id: appId }, function () {
      setTimeout(function () { window.close(); }, 500);
    });
  }, RETURN_DELAY);
}

/* --- status --- */

function probeCmd() {
  return 'echo ROOT_OK; ' +
    'echo PY:$( (python3 --version || python --version) 2>&1 | head -n 1 | tr " " _); ' +
    '[ -x ' + HOOK + ' ] && echo HOOK_OK || echo HOOK_NO; ' +
    'echo UNIT:$( (systemctl is-active ' + UNIT + ' 2>/dev/null) || ' +
    '(pgrep -f assets/bridge.py >/dev/null && echo active) || echo inactive); ' +
    'echo RUNNING:$(grep -o "Remote Bridge [0-9.]* started" ' + LOGFILE + ' 2>/dev/null | tail -n 1 | cut -d" " -f3); ' +
    'echo "---CONFIG---"; cat ' + CONFIG + ' 2>/dev/null; echo; ' +
    'echo "---LOG---"; tail -n 8 ' + LOGFILE + ' 2>/dev/null';
}

function refresh(cb) {
  rootExec(probeCmd(), function (r) {
    var out = (r && r.stdoutString) || '';
    if (!r || !r.returnValue || out.indexOf('ROOT_OK') < 0) {
      setDot('root', 'bad', 'unavailable: is Homebrew Channel installed and running as root?');
      setDot('installed', '', 'unknown');
      setDot('unit', '', 'unknown');
      setDot('kodi', '', 'unknown');
      log(JSON.stringify(r));
      if (cb) cb(false);
      return;
    }
    setDot('root', 'ok', 'available');
    var py = ((out.match(/PY:(\S*)/) || [])[1] || 'missing').replace(/_/g, ' ');
    var unit = (out.match(/UNIT:(\S*)/) || [])[1] || 'unknown';
    var running = (out.match(/RUNNING:(\S+)/) || [])[1];
    var conf = null;
    try { conf = JSON.parse(out.split('---CONFIG---')[1].split('---LOG---')[0]); } catch (e) { conf = null; }
    var hook = out.indexOf('HOOK_OK') >= 0;

    setDot('installed', hook ? 'ok' : 'warn', hook ? 'installed (' + py + ')' : 'not set up');
    setDot('unit', unit === 'active' ? 'ok' : 'warn',
           unit === 'active' ? 'running' + (running ? ' (' + running + ')' : '') : unit);
    setDot('kodi', conf ? 'ok' : 'warn',
           conf ? conf.host + ':' + conf.port : 'not set: set it up from Kodi (LG TV settings)');
    log((out.split('---LOG---')[1] || '').trim() || '(no log yet)');
    if (cb) cb(hook && unit === 'active');
  });
}

/* --- setup / removal (driven by Kodi) --- */

function setup(p) {
  headline('Setting up for Kodi at ' + p.kodiHost + '…');
  var conf = JSON.stringify({ host: String(p.kodiHost), port: Number(p.kodiPort), token: String(p.token) });
  var cmd = [
    'mkdir -p /var/lib/webosbrew/init.d',
    'printf %s ' + sq(conf) + ' > ' + CONFIG,
    'chmod 600 ' + CONFIG,
    'ln -sfn ' + ASSETS + '/97-kodiremote ' + HOOK,
    'sh ' + HOOK,
    'echo APPLIED'
  ].join('; ');
  rootExec(cmd, function (r) {
    if (output(r).indexOf('APPLIED') < 0) {
      headline('Setup failed');
      log(output(r) || JSON.stringify(r));
      return;
    }
    waitRunning(5, function (ok) {
      headline(ok ? 'Done: returning to Kodi' : 'Set up, but the bridge is not running (see the log)');
      if (ok) returnTo(p.returnTo);
    });
  });
}

function waitRunning(tries, cb) {
  refresh(function (ok) {
    if (ok || tries <= 1) { cb(ok); return; }
    setTimeout(function () { waitRunning(tries - 1, cb); }, 1500);
  });
}

function remove(p) {
  headline('Removing…');
  var cmd = [
    'systemctl stop ' + UNIT + ' 2>/dev/null',
    'pkill -f assets/bridge.py 2>/dev/null',
    'rm -f ' + HOOK + ' ' + CONFIG + ' /run/systemd/system/' + UNIT,
    'systemctl daemon-reload 2>/dev/null',
    'echo REMOVED'
  ].join('; ');
  rootExec(cmd, function (r) {
    var ok = output(r).indexOf('REMOVED') >= 0;
    headline(ok ? 'Removed: returning to Kodi' : 'Removal failed');
    refresh();
    if (ok) returnTo(p.returnTo);
  });
}

/* The daemon restarts itself after an app update; this is for anything else. */
function restart() {
  headline('Restarting the bridge…');
  rootExec('(systemctl restart ' + UNIT + ' 2>/dev/null || sh ' + HOOK + ') && echo RESTARTED', function (r) {
    if (output(r).indexOf('RESTARTED') < 0) {
      headline('Restart failed');
      log(output(r) || JSON.stringify(r));
      return;
    }
    setTimeout(function () {
      waitRunning(5, function (ok) {
        headline(ok ? 'Remote Bridge for Kodi' : 'The bridge did not come back (see the log)');
      });
    }, 1500);
  });
}

function showVersion() {
  var req = new XMLHttpRequest();
  req.onload = function () {
    try {
      document.getElementById('version').textContent = ' · version ' + JSON.parse(req.responseText).version;
    } catch (e) { /* no version shown */ }
  };
  req.open('GET', 'appinfo.json');
  req.send();
}

/* Homebrew Channel installs it over this app; the daemon restarts itself on
 * the new files. Back to Kodi right away: the install may close this app. */
function update(p) {
  headline('Updating the Remote Bridge…');
  luna('luna://org.webosbrew.hbchannel.service/install',
       { ipkUrl: String(p.ipkUrl), ipkHash: String(p.ipkHash), subscribe: true }, function (r) {
    if (r && r.returnValue === false) {
      headline('Update failed');
      log(r.errorText || JSON.stringify(r));
      return;
    }
    if (r && r.statusText) log(r.statusText);
    if (r && r.finished) {
      headline('Updated: returning to Kodi');
      returnTo(p.returnTo);
    }
  });
}

function handleParams(p) {
  if (p && p.action === 'remove') { remove(p); return; }
  if (p && p.action === 'update' && p.ipkUrl && p.ipkHash) { update(p); return; }
  if (p && p.kodiHost && p.kodiPort && p.token) { setup(p); return; }
  headline('Remote Bridge for Kodi');
  refresh();
}

function launchParams() {
  try { return JSON.parse(window.PalmSystem.launchParams || '{}'); } catch (e) { return {}; }
}

document.addEventListener('webOSRelaunch', function (e) { handleParams(e.detail || {}); });

document.getElementById('btn-refresh').addEventListener('click', function () { refresh(); });
document.getElementById('btn-restart').addEventListener('click', function () { restart(); });
document.getElementById('btn-remove').addEventListener('click', function () { remove({}); });

var buttons = ['btn-refresh', 'btn-restart', 'btn-remove'];
var focusIdx = 0;
function focusBtn(i) {
  focusIdx = (i + buttons.length) % buttons.length;
  document.getElementById(buttons[focusIdx]).focus();
}
document.addEventListener('keydown', function (e) {
  if (e.keyCode === 37) { focusBtn(focusIdx - 1); e.preventDefault(); }
  else if (e.keyCode === 39) { focusBtn(focusIdx + 1); e.preventDefault(); }
});

window.addEventListener('load', function () {
  showVersion();
  focusBtn(0);
  handleParams(launchParams());
});
