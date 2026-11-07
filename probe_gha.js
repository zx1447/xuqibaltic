// 在 GitHub runner 上跑：测 GitHub 出口 IP 能否通过 Turnstile（headless + headed/Xvfb 都试）
const { chromium } = require('playwright-core');
(async () => {
  console.log('出口 IP:', await (await fetch('https://api.ipify.org')).text().catch(()=>'?'));
  for (const headed of [false, true]) {
    const browser = await chromium.launch({ headless: !headed, args: ['--no-sandbox','--disable-blink-features=AutomationControlled'] });
    const ctx = await browser.newContext({ locale:'en-US', viewport:{width:1280,height:900},
      userAgent:'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36' });
    const page = await ctx.newPage();
    await page.goto('https://client.octagoncircle.org/auth/login', { waitUntil:'domcontentloaded', timeout:60000 });
    await page.waitForTimeout(8000);
    const box = await page.evaluate(() => {
      const inp = document.querySelector('[name="cf-turnstile-response"]');
      let el = inp;
      for (let i=0;i<6 && el;i++){ el = el.parentElement; if (el && el.getBoundingClientRect().height > 50){const r=el.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};} }
      return null;
    });
    if (box) { const cx=box.x+22, cy=box.y+box.h/2; await page.mouse.move(cx-40,cy-10,{steps:8}); await page.mouse.move(cx,cy,{steps:10}); await page.waitForTimeout(200); await page.mouse.click(cx,cy); }
    let tok='';
    for (let i=0;i<6;i++){ await page.waitForTimeout(5000);
      tok = await page.evaluate(()=>{const e=document.querySelector('[name="cf-turnstile-response"]');return e&&e.value?e.value:'';});
      console.log(`  [${headed?'headed':'headless'}] t+${(i+1)*5}s token=${tok?('有(len '+tok.length+')'):'空'}`);
      if (tok) break; }
    await page.screenshot({path:`turnstile-${headed?'headed':'headless'}.png`});
    await browser.close();
    if (tok) { console.log('✅ 该模式下拿到 token'); break; }
  }
})();
