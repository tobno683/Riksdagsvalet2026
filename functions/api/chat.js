// functions/api/chat.js
//
// A Cloudflare Pages Function. Deploys automatically at /api/chat alongside
// the rest of the static site — same domain, so no CORS setup needed.
//
// The Anthropic API key lives ONLY as a Cloudflare secret (set via the
// dashboard: Settings > Environment variables and secrets > Add > Encrypt),
// never in this file, never in the repo. It's read here via context.env and
// never sent back to the browser.

const SYSTEM_PROMPT = `Du är en chattassistent inbäddad på en webbplats om det svenska riksdagsvalet 2026 och Sveriges åtta riksdagspartier.

DITT ÄMNESOMRÅDE — svara bara på frågor om:
- Svensk politik: riksdagspartierna, deras historia, vallöften, regeringsbildning, voteringar, opinionsläge
- Riksdagsvalet 2026 specifikt: datum, praktiska detaljer, debatter, valsystemet
- Svenska politiska institutioner: Riksdagen, regeringen, val, folkomröstningar
- Innehållet som redan finns på den här webbplatsen, om användaren frågar om det

OM NÅGON FRÅGAR OM NÅGOT ANNAT (väder, kodning, andra länders politik, allmänna kunskapsfrågor, personliga råd, etc.):
Svara kort och vänligt att du bara kan hjälpa till med frågor om svensk politik och riksdagsvalet 2026, och erbjud att svara på en sådan fråga istället. Var inte pedantisk eller upprepa avvisandet i onödan — bara en kort, artig omdirigering en gång, sedan vidare.

VIKTIGA PRINCIPER, i linje med resten av sajten:
- Var strikt opartisk. Rekommendera aldrig ett parti framför ett annat, och undvik att lägga in egna värderingar om vilket parti som "har rätt".
- Presentera fakta om olika partiers ståndpunkter sida vid sida när det är relevant, utan att rangordna dem.
- Skilj tydligt mellan (a) vad ett parti lovar/säger om sig själv, och (b) oberoende verifierbara fakta. Var tydlig när något är ett parti eget påstående.
- Om du är osäker på en exakt siffra eller ett specifikt beslutsdatum, säg det istället för att gissa.
- Håll svaren relativt korta och lättlästa i en chattruta — sikta på några meningar till en kort paragraf, inte långa uppsatser, om inte användaren uttryckligen ber om mer detalj.
- Svara på svenska om inte användaren skriver på ett annat språk.`;

export async function onRequestPost(context) {
  const { request, env } = context;

  let body;
  try {
    body = await request.json();
  } catch (err) {
    return jsonResponse({ error: "Ogiltig förfrågan." }, 400);
  }

  const incoming = Array.isArray(body?.messages) ? body.messages : null;
  if (!incoming || incoming.length === 0) {
    return jsonResponse({ error: "Inga meddelanden skickades." }, 400);
  }

  // Basic abuse/cost guardrails: cap conversation length and message size.
  // (For real production traffic, also add a Cloudflare rate limiting rule
  // in the dashboard under Security > WAF > Rate limiting rules — that's
  // configured outside code, so it's not duplicated here.)
  const messages = incoming
    .slice(-12)
    .map((m) => ({
      role: m.role === "assistant" ? "assistant" : "user",
      content: String(m.content ?? "").slice(0, 2000),
    }))
    .filter((m) => m.content.trim().length > 0);

  if (messages.length === 0) {
    return jsonResponse({ error: "Tomt meddelande." }, 400);
  }

  if (!env.ANTHROPIC_API_KEY) {
    return jsonResponse(
      { error: "Servern är inte konfigurerad ännu (saknar API-nyckel)." },
      500
    );
  }

  let anthropicRes;
  try {
    anthropicRes = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-sonnet-5",
        max_tokens: 600,
        system: SYSTEM_PROMPT,
        messages,
      }),
    });
  } catch (err) {
    return jsonResponse({ error: "Kunde inte nå Claude just nu." }, 502);
  }

  if (!anthropicRes.ok) {
    // Don't leak upstream error details (could include sensitive info) to the browser.
    return jsonResponse(
      { error: "Något gick fel när svaret hämtades. Försök igen om en stund." },
      502
    );
  }

  const data = await anthropicRes.json();
  const textBlock = (data.content || []).find((b) => b.type === "text");
  const reply = textBlock?.text || "Jag kunde tyvärr inte svara just nu.";

  return jsonResponse({ reply });
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
