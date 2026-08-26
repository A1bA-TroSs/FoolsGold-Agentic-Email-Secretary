import { RefreshIcon } from './Icons.jsx';

/* Deliberately tiny markdown renderer -- the digest prompt only ever produces
   bullets, bold and italics, and pulling in a parser for that is not worth it. */
function render(md) {
  const lines = (md || '').split('\n');
  const out = [];
  let bullets = [];
  const inline = (s) => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>');

  const flush = () => {
    if (bullets.length) {
      out.push(`<ul>${bullets.map((b) => `<li>${inline(b)}</li>`).join('')}</ul>`);
      bullets = [];
    }
  };
  for (const line of lines) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) bullets.push(bullet[1]);
    else { flush(); if (line.trim()) out.push(`<p>${inline(line)}</p>`); }
  }
  flush();
  return out.join('');
}

export default function Digest({ digest, loading, onRefresh }) {
  return (
    <div className="digest">
      <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        What&rsquo;s crucial today
        <button className="btn ghost" style={{ marginLeft: 'auto', padding: '2px 6px' }}
                onClick={onRefresh} disabled={loading} title="Regenerate the digest">
          <RefreshIcon spinning={loading} />
        </button>
      </h3>
      {loading && !digest ? (
        <div style={{ color: 'var(--muted)', fontSize: 13 }}><span className="spin" /> Reading your inbox…</div>
      ) : (
        <div className="body" dangerouslySetInnerHTML={{ __html: render(digest?.body) }} />
      )}
      {digest?.model && digest.model !== 'none' && (
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          {digest.model === 'structural' ? 'Built without AI' : `via ${digest.model}`}
        </div>
      )}
    </div>
  );
}
