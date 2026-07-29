#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');
const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

const BASE = 'https://scyed.com';
const LOGIN_URL = `${BASE}/en/login`;
const RENEW_URL = `${BASE}/en/gameserver/6100ef84/upgrade/freeServer`;
const SESSION_URL = `${BASE}/api/auth/get-session`;
const STATE_FILE = 'scyed_state.json';
const USER = process.env.SCYED_USER || '';
const PASSWORD = process.env.SCYED_PASS || '';
const FORCE_RUN = String(process.env.FORCE_RUN || '').toLowerCase() === 'true';
const PROXY = process.env.SCYED_PROXY || process.env.HTTP_PROXY || '';
const PORT = 9222;
const PROFILE = '/tmp/scyed_chrome_profile';

function log(x) { console.log(x); }
function now() { return new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }); }
function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }
function isCf(title, html) { const x = `${title} ${html}`.toLowerCase(); return x.includes('just a moment') || x.includes('attention required') || x.includes('cf-chl-'); }
function isHardBlock(title, html) { const x = `${title} ${html}`.toLowerCase(); return x.includes('sorry, you have been blocked') || x.includes('unable to access scyed.com'); }
function portOpen(port) { return new Promise(resolve => { const req=http.get(`http://127.0.0.1:${port}/json/version`,()=>resolve(true)); req.on('error',()=>resolve(false)); req.setTimeout(1500,()=>{req.destroy();resolve(false);}); }); }
async function launchChrome() {
  if (await portOpen(PORT)) return;
  fs.rmSync(PROFILE, { recursive: true, force: true });
  const chrome = process.env.CHROME_PATH || '/usr/bin/google-chrome';
  const args = [
    `--remote-debugging-port=${PORT}`,
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run', '--no-default-browser-check', '--no-sandbox',
    '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled',
    '--lang=en-US', `--window-size=1280,720`, `--user-data-dir=${PROFILE}`,
  ];
  if (PROXY) {
    try {
      // URL.origin 对 socks5:// 会返回 "null"，必须把完整代理地址交给 Chrome。
      const parsed = new URL(PROXY);
      const proxy = PROXY.replace(/^socks5h:/i, 'socks5:');
      if (!['http:', 'https:', 'socks4:', 'socks5:'].includes(parsed.protocol)) throw new Error('unsupported proxy scheme');
      args.push(`--proxy-server=${proxy}`);
      log(`🔗 使用 ${parsed.protocol.replace(':', '').toUpperCase()} 代理出口`);
    } catch {
      throw new Error('SCYED_PROXY 格式无法解析');
    }
  }
  const p = spawn(chrome, args, { detached: true, stdio: 'ignore' }); p.unref();
  for (let i=0;i<30;i++) { if (await portOpen(PORT)) return; await new Promise(r=>setTimeout(r,1000)); }
  throw new Error('Chrome CDP 启动失败');
}
async function waitCf(page) {
  for (let i=0;i<12;i++) {
    await page.waitForTimeout(5000);
    const title=await page.title(); const html=await page.content();
    if (isHardBlock(title, html)) {
      throw new Error(`SCYED Cloudflare 已封锁当前出口 IP（title=${title}）`);
    }
    if (!isCf(title, html)) {
      log(`✅ Cloudflare 自动验证通过（等待约 ${(i+1)*5} 秒，title=${title}）`);
      return;
    }
    log(`🛡️ 等待 SCYED Cloudflare 自动验证（${i+1}/12，title=${title}）`);
  }
  throw new Error(`SCYED Cloudflare 自动验证未通过（最终 URL=${page.url()}）`);
}
async function sessionValid(page) {
  const r=await page.evaluate(async u=>{const x=await fetch(u,{credentials:'include'});return {status:x.status,text:await x.text()};}, SESSION_URL);
  if (r.status!==200) return false;
  try { const d=JSON.parse(r.text); return Boolean(d.user||d.session); } catch { return false; }
}
async function login(page) {
  await page.goto(LOGIN_URL,{waitUntil:'domcontentloaded',timeout:60000});
  await waitCf(page);
  const email=page.locator('input[type="email"],input[name="email"],#email,input[name="username"],#username,input[name="user"]').first();
  const pass=page.locator('input[type="password"],input[name="password"],#password').first();
  if (!(await email.count()) || !(await pass.count())) throw new Error('Cloudflare 后仍未找到登录输入框');
  log('🔑 Cloudflare 通过，填写 SCYED 账号密码');
  await email.fill(USER); await pass.fill(PASSWORD);
  const btn=page.locator('button[type="submit"],input[type="submit"],button.btn-primary').first();
  if (!(await btn.count())) throw new Error('未找到 SCYED 登录按钮');
  await btn.click(); await page.waitForTimeout(6000);
  if (!(await sessionValid(page))) throw new Error('/api/auth/get-session 未确认登录');
  log('✅ SCYED 登录和 get-session 验证成功');
}
async function renew(page) {
  const r=await page.evaluate(async u=>{const x=await fetch(u,{method:'POST',credentials:'include',headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});return {status:x.status,text:await x.text()};},RENEW_URL);
  log(`📡 POST ${RENEW_URL} -> HTTP ${r.status}`); log(`📦 返回：${r.text.slice(0,500)}`);
  if (r.status!==200) throw new Error(`续期失败 HTTP ${r.status}`);
}
async function main() {
  log('🚀 SCYED Playwright-Stealth 浏览器续期启动'); log(`🕐 北京时间：${now()}`);
  if (!USER || !PASSWORD) throw new Error('缺少 SCYED_USER/SCYED_PASS');
  const state=loadState(); const next=Number(state.next_renew_timestamp||0);
  if (!FORCE_RUN && next && Date.now()/1000<next) { log(`⏳ 尚未到随机续期时间：${state.next_renew_time||'未知'}`); return; }
  await launchChrome();
  const browser=await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
  const context=browser.contexts()[0] || await browser.newContext();
  const page=context.pages()[0] || await context.newPage();
  try {
    await login(page); await renew(page);
    const days=Math.floor(Math.random()*10)+5; const nextTs=Math.floor(Date.now()/1000)+days*86400;
    state.last_renew_time=now(); state.next_interval_days=days; state.next_renew_timestamp=nextTs; state.next_renew_time=new Date(nextTs*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}); saveState(state);
    log(`🎲 下次 SCYED 随机续期：${days} 天后`);
  } finally { await browser.close(); }
}
main().catch(e=>{console.error(`❌ SCYED 续期失败：${e.message}`);process.exitCode=1;});
