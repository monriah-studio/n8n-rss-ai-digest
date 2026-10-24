import json, uuid
def nid(s): return str(uuid.uuid5(uuid.NAMESPACE_URL, "n8n-rss-ai-digest/"+s))

CONFIG_JS = r'''// ===== EDIT ME =====
// Feeds to read (RSS 2.0 or Atom). Add or remove lines.
const feedUrls = [
  'https://hnrss.org/frontpage',
  'https://feeds.bbci.co.uk/news/technology/rss.xml',
  'https://www.theverge.com/rss/index.xml',
];

const config = {
  // Keep items whose title or snippet contains ANY of these words (case-insensitive).
  // Leave the list empty to keep everything.
  keywords: ['AI', 'open source', 'LLM'],
  // Ignore items older than this many hours (feeds without dates are kept).
  lookbackHours: 24,
  // Max items taken from each feed, and max items in one digest.
  maxItemsPerFeed: 15,
  maxItemsInDigest: 25,
  // Email settings (used by the "Send digest email" node).
  digestTitle: 'Daily RSS digest',
  emailTo: 'you@example.com',
  emailFrom: 'digest@example.com',
  // Webhook settings (used by the "Post to webhook" node, disabled by default).
  // Works with Slack/Discord/Teams incoming webhooks or any URL that accepts JSON.
  webhookUrl: 'https://example.com/your-webhook',
};
// ===== END EDIT ME =====

return feedUrls.map((url) => ({ json: { url, ...config } }));'''

NORMALIZE_JS = r'''// Clean up RSS items, drop failed feeds, keep only recent items that match a keyword.
const cfg = $('Config (edit me)').first().json;
// Whole-word, case-insensitive keyword match ("AI" matches "AI model", not "said").
const keywords = (cfg.keywords || []).map((k) => String(k).trim()).filter(Boolean)
  .map((k) => new RegExp('(^|[^\\p{L}\\p{N}])' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^\\p{L}\\p{N}])', 'iu'));
const cutoff = Date.now() - (Number(cfg.lookbackHours) || 24) * 3600 * 1000;
const perFeed = {};
const seen = new Set();
const out = [];

const strip = (s) => String(s || '').replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();

for (const [idx, item] of $input.all().entries()) {
  const j = item.json;
  if (j.error || !j.link || !j.title) continue;          // failed feed or empty item
  const link = String(j.link).trim();
  if (seen.has(link)) continue;                           // duplicate inside this run
  const published = j.isoDate || j.pubDate || null;
  const ts = published ? Date.parse(published) : NaN;
  if (!Number.isNaN(ts) && ts < cutoff) continue;         // too old
  const snippet = strip(j.contentSnippet || j.content || j.summary || '').slice(0, 400);
  const title = strip(j.title);
  const hay = title + ' ' + snippet;
  if (keywords.length && !keywords.some((re) => re.test(hay))) continue;
  // Which feed did this item come from? (paired item -> Config row), fallback: link host.
  let feedUrl = '';
  try { feedUrl = $('Config (edit me)').itemMatching(idx).json.url || ''; } catch (e) {}
  const host = (u) => ((String(u).match(/^https?:\/\/([^\/?#]+)/i) || [])[1] || '').replace(/^www\./, '');
  const source = host(feedUrl) || host(link);
  perFeed[feedUrl || source] = (perFeed[feedUrl || source] || 0) + 1;
  if (perFeed[feedUrl || source] > (Number(cfg.maxItemsPerFeed) || 15)) continue;
  seen.add(link);
  out.push({ json: { title, link, source, published: Number.isNaN(ts) ? null : new Date(ts).toISOString(), snippet } });
}
return out;'''

NORMALIZE_APIFY_JS = r'''// Map Apify rss-feed-monitor results to the digest format.
// The actor already filtered by keyword and returned only new items.
const cfg = $('Config (edit me)').first().json;
const cutoff = Date.now() - (Number(cfg.lookbackHours) || 24) * 3600 * 1000;
const seen = new Set();
const out = [];
for (const item of $input.all()) {
  const j = item.json;
  if (!j.link || !j.title) continue;
  if (seen.has(j.link)) continue;
  const ts = j.publishedAt ? Date.parse(j.publishedAt) : NaN;
  if (!Number.isNaN(ts) && ts < cutoff) continue;
  seen.add(j.link);
  const source = j.feedTitle || ((String(j.link).match(/^https?:\/\/([^\/?#]+)/i) || [])[1] || '').replace(/^www\./, '');
  out.push({ json: { title: j.title, link: j.link, source, published: Number.isNaN(ts) ? null : new Date(ts).toISOString(), snippet: String(j.description || '').slice(0, 400) } });
}
return out;'''

BUILD_PROMPT_JS = r'''// Sort newest first, cap the digest size and build one prompt for the LLM.
const cfg = $('Config (edit me)').first().json;
const items = $input.all().map((i) => i.json)
  .sort((a, b) => (b.published || '').localeCompare(a.published || ''))
  .slice(0, Number(cfg.maxItemsInDigest) || 25);
const list = items.map((it, n) => `[${n + 1}] ${it.title} (${it.source})\n${it.snippet}`).join('\n\n');
const prompt = `Here are ${items.length} new items from my RSS feeds.\n\n${list}\n\n` +
  `Write a short daily digest in plain text:\n` +
  `1. One sentence on the main theme of the day.\n` +
  `2. Then 3-7 bullet points ("- "), each grouping related items and citing them by number like [2][5].\n` +
  `Do not invent facts that are not in the items. No preamble.`;
return [{ json: { prompt, items, count: items.length } }];'''

FORMAT_JS = r'''// Turn the LLM text + item list into an HTML email and a JSON payload.
const cfg = $('Config (edit me)').first().json;
const { items, count } = $('Build prompt').first().json;
const summary = String($json.text || $json.output || '').trim();
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const date = new Date().toISOString().slice(0, 10);

const lines = summary.split('\n').map((l) => l.trim()).filter(Boolean);
let html = `<h2>${esc(cfg.digestTitle)} - ${date}</h2>`;
let inList = false;
for (const l of lines) {
  if (/^[-*•]\s+/.test(l)) {
    if (!inList) { html += '<ul>'; inList = true; }
    html += `<li>${esc(l.replace(/^[-*•]\s+/, ''))}</li>`;
  } else {
    if (inList) { html += '</ul>'; inList = false; }
    html += `<p>${esc(l)}</p>`;
  }
}
if (inList) html += '</ul>';
html += `<h3>Sources (${count})</h3><ol>` +
  items.map((it) => `<li><a href="${esc(it.link)}">${esc(it.title)}</a> <small>(${esc(it.source)})</small></li>`).join('') +
  '</ol><p><small>Sent by n8n-rss-ai-digest.</small></p>';

const text = `${cfg.digestTitle} - ${date}\n\n${summary}\n\nSources:\n` +
  items.map((it, n) => `[${n + 1}] ${it.title} - ${it.link}`).join('\n');

return [{ json: {
  subject: `${cfg.digestTitle} - ${date} (${count} items)`,
  html, text, summary, items, date,
  emailTo: cfg.emailTo, emailFrom: cfg.emailFrom, webhookUrl: cfg.webhookUrl,
} }];'''

SYSTEM = "You are a concise news editor. You summarise RSS items accurately, group related stories, and cite item numbers. You never add facts that are not in the items."

def code(name, js, pos, mode=None):
    p = {"jsCode": js}
    if mode: p["mode"] = mode
    return {"parameters": p, "id": nid(name), "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos}

def common_tail(x0):
    nodes = [
      code("Build prompt", BUILD_PROMPT_JS, [x0, 300]),
      {"parameters": {"promptType": "define", "text": "={{ $json.prompt }}",
                      "messages": {"messageValues": [{"message": SYSTEM}]}},
       "id": nid("Summarize with LLM"), "name": "Summarize with LLM", "type": "@n8n/n8n-nodes-langchain.chainLlm", "typeVersion": 1.7, "position": [x0+220, 300]},
      {"parameters": {"modelName": "models/gemini-3.1-flash-lite", "options": {"temperature": 0.3}},
       "id": nid("Gemini (free tier)"), "name": "Gemini (free tier)", "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "typeVersion": 1.2, "position": [x0+200, 520]},
      {"parameters": {"model": "openai/gpt-oss-120b", "options": {"temperature": 0.3}},
       "id": nid("Groq (free tier, optional)"), "name": "Groq (free tier, optional)", "type": "@n8n/n8n-nodes-langchain.lmChatGroq", "typeVersion": 1, "position": [x0+380, 520]},
      code("Format digest", FORMAT_JS, [x0+560, 300]),
      {"parameters": {"fromEmail": "={{ $json.emailFrom }}", "toEmail": "={{ $json.emailTo }}", "subject": "={{ $json.subject }}",
                      "emailFormat": "both", "text": "={{ $json.text }}", "html": "={{ $json.html }}", "options": {"appendAttribution": False}},
       "id": nid("Send digest email"), "name": "Send digest email", "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1, "position": [x0+800, 200], "webhookId": nid("email-webhook")},
      {"parameters": {"method": "POST", "url": "={{ $json.webhookUrl }}", "sendBody": True, "specifyBody": "json",
                      "jsonBody": "={{ JSON.stringify({ text: $json.text, content: $json.text.slice(0, 1900), subject: $json.subject, summary: $json.summary, items: $json.items, date: $json.date }) }}",
                      "options": {}},
       "id": nid("Post to webhook"), "name": "Post to webhook", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [x0+800, 400], "disabled": True},
    ]
    conns = {
      "Build prompt": {"main": [[{"node": "Summarize with LLM", "type": "main", "index": 0}]]},
      "Summarize with LLM": {"main": [[{"node": "Format digest", "type": "main", "index": 0}]]},
      "Gemini (free tier)": {"ai_languageModel": [[{"node": "Summarize with LLM", "type": "ai_languageModel", "index": 0}]]},
      "Format digest": {"main": [[{"node": "Send digest email", "type": "main", "index": 0}, {"node": "Post to webhook", "type": "main", "index": 0}]]},
    }
    return nodes, conns

def sticky(name, content, pos, w, h, color=None):
    p = {"content": content, "height": h, "width": w}
    if color: p["color"] = color
    return {"parameters": p, "id": nid(name), "name": name, "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": pos}

SETUP = ("## RSS → AI daily digest (free)\n"
 "1. Edit the feeds, keywords and email addresses in **Config (edit me)**.\n"
 "2. Open **Gemini (free tier)** and create a *Google Gemini (PaLM) API* credential with your own free key from Google AI Studio.\n"
 "3. Open **Send digest email** and add an SMTP credential (or disable it and enable **Post to webhook**).\n"
 "4. Click **Execute workflow** to test, then switch the workflow to **Active**.\n\n"
 "Free option B: connect **Groq (free tier, optional)** to the LLM node instead of Gemini.\n\n"
 "MIT license · github.com/monriah-studio/n8n-rss-ai-digest")
GROQ_NOTE = ("### Want Groq instead?\nDelete the link from Gemini, drag Groq's model connector onto **Summarize with LLM**, and add a Groq API credential (free key at console.groq.com). Only one model can be connected.")

def main_wf():
    sched = {"parameters": {"rule": {"interval": [{"field": "days", "triggerAtHour": 7}]}},
             "id": nid("Every day at 7:00"), "name": "Every day at 7:00", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [-660, 300]}
    cfg = code("Config (edit me)", CONFIG_JS, [-440, 300])
    rss = {"parameters": {"url": "={{ $json.url }}", "options": {}}, "id": nid("Read RSS feeds"), "name": "Read RSS feeds",
           "type": "n8n-nodes-base.rssFeedRead", "typeVersion": 1.2, "position": [-220, 300], "onError": "continueRegularOutput"}
    norm = code("Clean + keyword filter", NORMALIZE_JS, [0, 300])
    dd = {"parameters": {"operation": "removeItemsSeenInPreviousExecutions", "dedupeValue": "={{ $json.link }}", "options": {}},
          "id": nid("Skip items sent before"), "name": "Skip items sent before", "type": "n8n-nodes-base.removeDuplicates", "typeVersion": 2, "position": [220, 300]}
    tail, conns = common_tail(440)
    nodes = [sticky("Setup", SETUP, [-700, -60], 620, 320), sticky("Groq note", GROQ_NOTE, [600, 660], 420, 160, 5), sched, cfg, rss, norm, dd] + tail
    conns.update({
      "Every day at 7:00": {"main": [[{"node": "Config (edit me)", "type": "main", "index": 0}]]},
      "Config (edit me)": {"main": [[{"node": "Read RSS feeds", "type": "main", "index": 0}]]},
      "Read RSS feeds": {"main": [[{"node": "Clean + keyword filter", "type": "main", "index": 0}]]},
      "Clean + keyword filter": {"main": [[{"node": "Skip items sent before", "type": "main", "index": 0}]]},
      "Skip items sent before": {"main": [[{"node": "Build prompt", "type": "main", "index": 0}]]},
    })
    return {"name": "RSS → AI daily digest (free)", "nodes": nodes, "connections": conns, "pinData": {},
            "settings": {"executionOrder": "v1"}, "meta": {"templateCredsSetupCompleted": False}, "tags": []}

APIFY_CFG = CONFIG_JS.replace("return feedUrls.map((url) => ({ json: { url, ...config } }));",
  "// One item with everything the Apify actor needs.\nreturn [{ json: { feedUrls, ...config } }];")
def apify_wf():
    sched = {"parameters": {"rule": {"interval": [{"field": "days", "triggerAtHour": 7}]}},
             "id": nid("A Every day at 7:00"), "name": "Every day at 7:00", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [-660, 300]}
    cfg = code("Config (edit me)", APIFY_CFG, [-440, 300]); cfg["id"] = nid("A config")
    http = {"parameters": {"method": "POST",
            "url": "https://api.apify.com/v2/acts/monriah-studio~rss-feed-monitor/run-sync-get-dataset-items",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ feedUrls: $json.feedUrls, keywords: $json.keywords, maxItemsPerFeed: $json.maxItemsPerFeed, onlyNewSinceLastRun: true, stateStoreName: 'n8n-rss-ai-digest', includeContent: false }) }}",
            "options": {"timeout": 120000}},
            "id": nid("A Apify"), "name": "Apify: rss-feed-monitor", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [-220, 300]}
    norm = code("Map Apify items", NORMALIZE_APIFY_JS, [0, 300]); norm["id"] = nid("A map")
    tail, conns = common_tail(440)
    for n in tail: n["id"] = nid("A " + n["name"])
    note = ("## Variant: RSS via Apify actor\nUses the public Apify actor **monriah-studio/rss-feed-monitor** to fetch feeds, filter keywords and return only items it hasn't returned before (change detection).\n\n"
            "1. Create a *Header Auth* credential on **Apify: rss-feed-monitor**: Name `Authorization`, Value `Bearer YOUR_APIFY_TOKEN`.\n"
            "2. Then follow steps 1-4 of the main workflow.\n\n"
            "Cost: the actor is pay-per-result ($1.50 per 1,000 items, checked Oct 24, 2026), billed to your Apify account; Apify's free plan includes monthly credit. The main workflow.json needs no Apify account.")
    nodes = [sticky("Setup", note, [-700, -60], 620, 340), sticky("Groq note", GROQ_NOTE, [600, 660], 420, 160, 5), sched, cfg, http, norm] + tail
    nodes[0]["id"] = nid("A setup"); nodes[1]["id"] = nid("A groq")
    conns.update({
      "Every day at 7:00": {"main": [[{"node": "Config (edit me)", "type": "main", "index": 0}]]},
      "Config (edit me)": {"main": [[{"node": "Apify: rss-feed-monitor", "type": "main", "index": 0}]]},
      "Apify: rss-feed-monitor": {"main": [[{"node": "Map Apify items", "type": "main", "index": 0}]]},
      "Map Apify items": {"main": [[{"node": "Build prompt", "type": "main", "index": 0}]]},
    })
    return {"name": "RSS → AI daily digest (Apify variant)", "nodes": nodes, "connections": conns, "pinData": {},
            "settings": {"executionOrder": "v1"}, "meta": {"templateCredsSetupCompleted": False}, "tags": []}

json.dump(main_wf(), open("workflow.json", "w"), indent=2, ensure_ascii=False)
json.dump(apify_wf(), open("workflow-apify.json", "w"), indent=2, ensure_ascii=False)
print("ok")
