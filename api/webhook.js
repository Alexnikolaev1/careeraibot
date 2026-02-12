/**
 * Первый обработчик webhook Telegram (Node.js).
 * Отвечает на pre_checkout_query сразу (без холодного старта Python).
 * Остальные апдейты пересылаются в Python (/api/webhook-handler).
 * v2: прогревочный запрос (update_id=0) не пересылается в Python.
 */
module.exports = async function handler(req, res) {
  const method = (req.method || '').toUpperCase();
  console.log('[WEBHOOK_JS] v2 DEPLOYED_FEB12', method, req.url);
  // GET — для прогрева (браузер, cron). Всегда 200.
  if (method === 'GET') {
    res.status(200).json({ ok: true });
    return;
  }
  if (method !== 'POST') {
    try {
      if (typeof res.writeHead === 'function') {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false }));
      } else {
        res.status(405).json({ ok: false });
      }
    } catch (_) {}
    return;
  }

  let update = req.body;
  if (typeof update === 'string') {
    try {
      update = JSON.parse(update);
    } catch (e) {
      return res.status(400).json({ ok: false });
    }
  }
  if (!update || typeof update !== 'object') {
    return res.status(400).json({ ok: false });
  }

  // Прогрев: update_id 0 или "0" — только наш cron (Telegram всегда шлёт положительный update_id)
  const uid = update.update_id;
  const isWarm = uid === 0 || uid === '0';
  console.log('[WEBHOOK_JS] update_id=', uid, 'isWarm=', isWarm);
  if (isWarm) {
    console.log('[WEBHOOK_JS] warm request, returning 200');
    res.setHeader('Content-Type', 'application/json');
    res.status(200).send(JSON.stringify({ ok: true }));
    return;
  }

  console.log('[WEBHOOK_JS] has pre_checkout=', !!update.pre_checkout_query);
  const pq = update.pre_checkout_query;
  if (pq) {
    console.log('[WEBHOOK_JS] pre_checkout_query received, pq_id=', pq.id);
    const token = process.env.BOT_TOKEN || '';
    const payload = (pq.invoice_payload || '').trim();
    const ok = payload.startsWith('premium_');
    const body = { pre_checkout_query_id: pq.id, ok };
    if (!ok) body.error_message = 'Неверный счёт. Используйте кнопку «Купить Premium» из бота.';

    try {
      const r = await fetch(`https://api.telegram.org/bot${token}/answerPreCheckoutQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        console.log('[WEBHOOK_JS] answerPreCheckoutQuery ok=true sent');
      } else {
        console.warn('[WEBHOOK_JS] answerPreCheckoutQuery', r.status, await r.text());
      }
    } catch (e) {
      console.error('[WEBHOOK_JS] answerPreCheckoutQuery error', e);
    }
    return res.status(200).json({ ok: true });
  }

  // Не pre_checkout — пересылаем в Python
  const host = req.headers.host || 'careeraibot.vercel.app';
  const url = `https://${host}/api/webhook-handler`;
  try {
    const proxyRes = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
    });
    const text = await proxyRes.text();
    res.setHeader('Content-Type', 'application/json');
    res.status(proxyRes.status).send(text);
  } catch (e) {
    console.error('webhook-handler fetch error', e);
    res.status(502).json({ ok: false });
  }
};
