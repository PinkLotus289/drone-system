// Against the loopback-only demo_ui_app fixture. No hardware endpoints exist.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [], forbidden = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/demo/flight')) {
      forbidden.push(url.pathname); return route.abort();
    }
    return route.continue();
  });
  const enabled = id => page.waitForFunction(id => !document.getElementById(id).disabled, id, {timeout: 20000});
  const modal = async id => {
    for (const box of await page.locator(`#${id} input[type=checkbox]`).all()) await box.check();
    await page.locator(`#${id} .pf-modal-foot .btn-primary`).click();
  };
  try {
    await page.goto('http://127.0.0.1:8073/real?demo=1');
    await page.waitForFunction(() => document.getElementById('pf-meta').textContent.includes('walkthrough'));
    assert(await page.locator('#btn-arm').isDisabled());
    await enabled('btn-arm');
    await page.selectOption('#demo-scenario', 'compass');
    await page.waitForFunction(() => document.getElementById('pf-verdict').textContent.includes('CALIB MAG'));
    assert(await page.locator('#btn-arm').isDisabled());
    await page.selectOption('#demo-scenario', 'healthy');
    await enabled('btn-arm');
    await page.locator('#btn-motor-test').click(); await modal('modal-motor');
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('motor_test: completed'));
    await enabled('btn-arm');
    await page.locator('#btn-arm').click(); await modal('modal-arm');
    await enabled('btn-test-takeoff');
    await page.locator('#btn-test-takeoff').click(); await modal('modal-hop');
    await page.waitForFunction(() => !document.querySelector('[data-step=forward]').disabled, null, {timeout: 15000});
    const before = await page.locator('#demo-drone').getAttribute('transform');
    await page.locator('[data-step=forward]').click();
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('test_step: completed'), null, {timeout: 10000});
    assert.notEqual(await page.locator('#demo-drone').getAttribute('transform'), before);
    await enabled('btn-test-return'); await page.locator('#btn-test-return').click();
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('test_return: completed'), null, {timeout: 10000});
    if (process.env.UI_SCREENSHOT) {
      await page.locator('#flight-check-grid').evaluate(el => el.closest('details').open = false);
      await page.screenshot({path: process.env.UI_SCREENSHOT, fullPage: true});
    }
    await page.locator('#btn-land').click();
    await page.waitForFunction(() => document.getElementById('pf-command').textContent.includes('land: completed'), null, {timeout: 10000});
    await page.selectOption('#demo-scenario', 'link_loss');
    await page.waitForFunction(() => document.getElementById('demo-status').textContent.includes('link lost'));
    assert(await page.locator('#btn-land').isVisible());
    await page.locator('#btn-demo-reset').click();
    await page.waitForFunction(() => document.getElementById('demo-status').textContent.includes('connected'));
    assert((await page.locator('#pf-state').textContent()).includes('armed: NO'));
    await page.reload();
    await page.waitForFunction(() => document.getElementById('pf-meta').textContent.includes('walkthrough'));
    assert.equal(forbidden.length, 0, forbidden.join(', '));
    assert.equal(errors.length, 0, errors.join('\n'));
    await page.request.delete('http://127.0.0.1:8073/api/demo/flight');
    console.log('Demo UI passed: readiness, fault/recovery, motor test, arm, takeoff, move, return, LAND, link loss, reset, reload; no hardware requests.');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
