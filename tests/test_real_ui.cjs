// Isolated browser checks: every HTTP request is intercepted; no broker or drone.
// Run with a local Playwright installation: node tests/test_real_ui.cjs
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const report = {bridge_id: 'test-bridge', ok: true, can_arm: true, can_disarm: true, flight_ready: true,
    armed: false, in_air: false, mode: 'HOLD', alt: 0, phase: 'idle', operation_busy: false,
    report_age_s: 0.1, checks: [{label: 'CONTINUOUS READINESS', value: '10 / 10 s', status: 'ok'}],
    flight_checks: [{label: 'RC BACKUP', value: 'available', status: 'ok'}], commands: [], reasons: [], status_text: []};
  let offline = false, rejectArm = true;
  const requests = [];
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if (u.hostname !== 'skybite.test') return route.abort();
    if (u.pathname === '/real') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(__dirname, '../src/web_ui/static/real_connect.html'), 'utf8')});
    if (u.pathname.startsWith('/static/')) {
      const f = path.join(__dirname, '../src/web_ui', u.pathname);
      return route.fulfill({contentType: f.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(f)});
    }
    if (u.pathname === '/api/real/profiles') return json({profiles: [{name: '11', display_name: 'Test quad', connection_type: 'radio'}]});
    if (u.pathname === '/api/real/connections') return json({connections: {'11': {connected: true, bridge_running: true}}, radio: {}});
    if (u.pathname === '/api/fleet') return json({fleet: []});
    if (u.pathname === '/api/real/preflight/11') return offline ? route.abort() : json(report);
    if (u.pathname === '/api/control/acquire') return json({ok: true});
    if (u.pathname === '/api/drone/veh_11/command') {
      const body = route.request().postDataJSON(); requests.push(body.action);
      if (body.action === 'arm' && rejectArm) return json({error: 'preflight_blocked', message: 'PX4 denied arm'}, 409);
      return json({ok: true, command_id: 'queued-command', status: 'queued'}, 202);
    }
    return route.abort();
  });
  try {
    await page.goto('http://skybite.test/real');
    await page.locator('.pcard').click();
    await page.waitForFunction(() => !document.getElementById('btn-arm').disabled);
    await page.locator('#btn-arm').click();
    for (const b of await page.locator('#modal-arm input').all()) await b.check();
    await page.locator('#confirm-arm').click();
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('PX4 denied arm'));
    assert((await page.locator('#pf-state').textContent()).includes('armed: NO'));
    assert.equal(await page.locator('#pf-override').count(), 0);
    // A queue acknowledgement is never a confirmed arm.
    rejectArm = false;
    await page.waitForFunction(() => !document.getElementById('btn-arm').disabled);
    await page.locator('#btn-arm').click();
    for (const b of await page.locator('#modal-arm input').all()) await b.check();
    await page.locator('#confirm-arm').click();
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('awaiting bridge'));
    assert((await page.locator('#pf-state').textContent()).includes('armed: NO'));
    // Lost HTTP/telemetry cannot hide or disable emergency LAND.
    offline = true;
    await page.waitForFunction(() => document.getElementById('pf-verdict').textContent.includes('STATE UNKNOWN'));
    await page.evaluate(() => scrollTo(0, 0));
    assert(await page.locator('#btn-land').isVisible());
    const box = await page.locator('#btn-land').boundingBox();
    assert(box.y > 0 && box.y + box.height < 1000);
    assert(await page.locator('#btn-arm').isDisabled());
    await page.locator('#btn-land').click();
    assert(requests.includes('land'));
    assert.equal(errors.length, 0, errors.join('\n'));
    if (process.env.UI_SCREENSHOT) await page.screenshot({path: process.env.UI_SCREENSHOT, fullPage: true});
    console.log('UI passed: server rejection, queued≠armed, unknown state, LAND visible/actionable, no JS errors.');
  } finally { await browser.close(); }
})().catch((e) => {console.error(e); process.exitCode = 1;});
