// Dinii 経営管理API から本部運営8店の日別KPI（売上・客数・天候）を取得して .cache/kpi.json に保存する。
//
//   node scripts/fetch_kpi.mjs                  … 開店日〜今日ぶんを取り直す
//   node scripts/fetch_kpi.mjs 2026-01-01       … 開始日を指定
//
// ログインセッションは ~/doteppan-shussu/.profile を借りる（出数ダッシュボードと同じもの）。
// 切れていたら `node ~/doteppan-shussu/scripts/browser.mjs` で開き直して本人がログインする。
import { chromium } from 'playwright';
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs/promises';

const ROOT = path.join(os.homedir(), 'doteppan-forecast');
const PROFILE = path.join(os.homedir(), 'doteppan-shussu', '.profile');
const GQL = 'https://hasura-query.self.dinii.jp/v1/graphql';
const START = process.argv[2] ?? '2025-10-01';

const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10); // JST
const END = today;

const KPI_QUERY = `query FlDashboardGetShopDailyBusinessKpi($corporationId: String!, $shopId: String!, $startAt: DateTime!, $endAt: DateTime!, $shouldUseDemoData: Boolean) {
  dailyShopBusinessKpi(input: {corporationId: $corporationId, shopId: $shopId, startAt: $startAt, endAt: $endAt, shouldUseDemoData: $shouldUseDemoData}) {
    dailyShopBusinessKpi { shopId businessDate totalTaxIncludedAmount numPeople weatherCondition salesTargetAmount }
  }
}`;

const ctx = await chromium.launchPersistentContext(PROFILE, {
  headless: true, viewport: { width: 1600, height: 1200 }, locale: 'ja-JP', timezoneId: 'Asia/Tokyo',
});
const page = ctx.pages()[0] ?? (await ctx.newPage());

// BIダッシュボードを一度開いて、APIに付いている認証ヘッダをそのまま借りる
let headers = null, corporationId = null;
page.on('request', (r) => {
  if (r.url() === GQL && !headers) headers = r.headers();
  const m = /"corporationId":"([0-9a-f-]{36})"/.exec(r.postData() || '');
  if (m && !corporationId) corporationId = m[1];
});
await page.goto('https://dashboard.self.dinii.jp/bi/flDashboard', { waitUntil: 'domcontentloaded', timeout: 60000 });
for (let i = 0; i < 40 && !(headers && corporationId); i++) await page.waitForTimeout(1000);
if (!headers || !corporationId) {
  await ctx.close();
  throw new Error('ログインが切れています。node ~/doteppan-shussu/scripts/browser.mjs で開いてログインし直してください');
}

const h = { 'content-type': 'application/json' };
for (const [k, v] of Object.entries(headers)) {
  if (k === 'authorization' || k === 'accept' || k.startsWith('x-')) h[k] = v;
}

async function gql(body) {
  const res = await page.request.post(GQL, { headers: h, data: body });
  const json = await res.json();
  if (json.errors) throw new Error(JSON.stringify(json.errors).slice(0, 300));
  return json.data;
}

const { shops: WANT } = JSON.parse(await fs.readFile(path.join(ROOT, 'scripts', 'events.json'), 'utf8'));

const data = await gql({
  operationName: 'GetCompaniesAndShops',
  variables: {},
  query: 'query GetCompaniesAndShops { company(where: {archivedAt: {_is_null: true}}) { corporationId shops(where: {archivedAt: {_is_null: true}}) { name shopId } } }',
});
const shops = [];
for (const c of data.company) {
  for (const s of c.shops) {
    const code = (s.name.match(/\d{6}/) ?? [])[0];
    if (code && WANT[code] && !s.name.includes('未使用')) shops.push({ code, name: s.name, shopId: s.shopId, corporationId: c.corporationId });
  }
}
console.log(`[kpi] 対象 ${shops.length}店 / ${START} 〜 ${END}`);

// 月ごとに区切って取る（1リクエストの期間が長すぎると落ちるため）
const months = [];
for (const d = new Date(START + 'T00:00:00'); d <= new Date(END + 'T00:00:00'); d.setMonth(d.getMonth() + 1)) {
  const s = new Date(d.getFullYear(), d.getMonth(), 1), e = new Date(d.getFullYear(), d.getMonth() + 1, 0);
  const f = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
  months.push([f(s) < START ? START : f(s), f(e) > END ? END : f(e)]);
}

const rows = [];
for (const s of shops) {
  for (const [a, b] of months) {
    try {
      const d = await gql({
        operationName: 'FlDashboardGetShopDailyBusinessKpi',
        variables: { corporationId: s.corporationId, shopId: s.shopId, startAt: a, endAt: b, shouldUseDemoData: false },
        query: KPI_QUERY,
      });
      for (const r of d.dailyShopBusinessKpi?.dailyShopBusinessKpi ?? []) {
        if (!r.totalTaxIncludedAmount) continue;
        rows.push({ code: s.code, date: r.businessDate, sales: r.totalTaxIncludedAmount, people: r.numPeople ?? 0, weather: r.weatherCondition ?? null, target: r.salesTargetAmount ?? null });
      }
    } catch (err) {
      console.log(`  !! ${s.name} ${a}: ${String(err.message).slice(0, 120)}`);
    }
  }
  process.stdout.write('.');
}
console.log(`\n[kpi] ${rows.length}行`);

await fs.mkdir(path.join(ROOT, '.cache'), { recursive: true });
await fs.writeFile(path.join(ROOT, '.cache', 'kpi.json'), JSON.stringify({ fetchedAt: new Date().toISOString(), rows }));
await ctx.close();
