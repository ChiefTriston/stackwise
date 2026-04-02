const stackwiseTools = [
  { name:"Cursor", cat:"Code editor", verdict:"Worth it for daily devs at $20/mo", href:"cursor.html" },
  { name:"Grammarly", cat:"Writing assistant", verdict:"Strong free tier, solid for professionals", href:"grammarly.html" },
  { name:"Perplexity AI", cat:"AI search", verdict:"Best free search with real citations", href:"perplexity-ai.html" },
  { name:"Notion AI", cat:"Workspace AI", verdict:"Strong if you're already in Notion", href:"notion-ai.html" },
  { name:"Otter.ai", cat:"Meeting transcription", verdict:"Good free tier for solo use", href:"otter-ai.html" },
  { name:"LangChain", cat:"Developer framework", verdict:"Best for complex agent workflows", href:"langchain.html" },
  { name:"Lindy AI", cat:"AI automation", verdict:"Best no-code agent builder", href:"lindy-ai.html" },
  { name:"Glean", cat:"Enterprise search", verdict:"Best for large team knowledge search", href:"glean.html" },
  { name:"OpenClaw", cat:"Open-source AI agent", verdict:"Powerful but requires technical setup", href:"openclaw.html" },
  { name:"Proactor AI", cat:"Project management", verdict:"Limited coverage — check official site", href:"proactor-ai.html" },
  { name:"ChatGPT", cat:"AI assistant", verdict:"Most versatile AI assistant", href:"chatgpt.html" },
  { name:"Writesonic", cat:"AI writing platform", verdict:"Strong for SEO content", href:"writesonic.html" },
  { name:"QuillBot", cat:"Writing assistant", verdict:"Best for paraphrasing and grammar", href:"quillbot.html" },
  { name:"GetGenie", cat:"AI writing platform", verdict:"Best WordPress-integrated AI content tool", href:"getgenie.html" },
  { name:"InVideo", cat:"Video creation", verdict:"Best for beginners making marketing videos", href:"invideo.html" },
  { name:"HeadshotPro", cat:"Image generation", verdict:"Best AI headshot generator", href:"headshotpro.html" },
  { name:"AISEO", cat:"SEO & content", verdict:"Best for on-page SEO content", href:"aiseo.html" },
  { name:"NeuronWriter", cat:"SEO & content", verdict:"Best for SEO-optimized long-form content", href:"neuronwriter.html" },
  { name:"Synthesia", cat:"Video creation", verdict:"Best AI avatar video generator", href:"synthesia.html" },
  { name:"HeyGen", cat:"Video creation", verdict:"Best for realistic avatar videos", href:"heygen.html" },
  { name:"Leonardo.ai", cat:"Image & video", verdict:"Best for high-quality marketing visuals", href:"leonardo-ai.html" },
  { name:"AdCreative.ai", cat:"Ad creative", verdict:"Best for generating 150+ ad variations", href:"adcreative-ai.html" },
  { name:"GetResponse", cat:"Email marketing", verdict:"Best all-in-one email & automation tool", href:"getresponse.html" }
];

const searchInput = document.getElementById('globalSearch');
const searchResults = document.getElementById('searchResults');

function escapeHtml(value) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function buildSearchResults(query) {
  if (!searchInput || !searchResults) return;

  const normalized = query.toLowerCase().trim();

  if (normalized.length < 2) {
    searchResults.style.display = 'none';
    searchResults.innerHTML = '';
    return;
  }

  const filtered = stackwiseTools.filter(tool =>
    tool.name.toLowerCase().includes(normalized) ||
    tool.cat.toLowerCase().includes(normalized) ||
    tool.verdict.toLowerCase().includes(normalized)
  );

  if (!filtered.length) {
    searchResults.innerHTML = `<div class="search-empty">No matches found for "${escapeHtml(query)}"</div>`;
    searchResults.style.display = 'block';
    return;
  }

  searchResults.innerHTML = filtered
    .slice(0, 8)
    .map(tool => `
      <a href="${tool.href}" class="search-result-item">
        <div class="search-result-name">${tool.name}</div>
        <div class="search-result-desc">${tool.cat} · ${tool.verdict}</div>
      </a>
    `)
    .join('');

  searchResults.style.display = 'block';
}

if (searchInput && searchResults) {
  searchInput.addEventListener('input', e => buildSearchResults(e.target.value));

  searchInput.addEventListener('focus', e => {
    if (e.target.value.trim().length >= 2) {
      buildSearchResults(e.target.value);
    }
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('.nav-search-wrap')) {
      searchResults.style.display = 'none';
    }
  });

  searchInput.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      searchResults.style.display = 'none';
      searchInput.blur();
    }
  });
}