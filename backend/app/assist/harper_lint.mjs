// Harper (Apache-2.0) as a one-shot linter: JSON on stdin, JSON on stdout.
//
// Runs entirely on-device -- Harper is a WASM grammar checker with no network
// of any kind, which is why it is here rather than LanguageTool (a Java
// server, and no Korean anyway). This process is spawned per check and exits;
// nothing is kept warm, because a 300-word email lints in milliseconds.
import { LocalLinter, Dialect } from 'harper.js';
import { binaryInlined } from 'harper.js/binaryInlined';

const input = await new Promise((resolve) => {
  let buffer = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { buffer += chunk; });
  process.stdin.on('end', () => resolve(buffer));
});

const { text = '', kinds = [], dialect = '' } = JSON.parse(input || '{}');
// American is Harper's own default; the others exist because "fulfilment" and
// "organise" are correct where this app's first user lives.
const DIALECTS = {
  american: Dialect.American, british: Dialect.British,
  canadian: Dialect.Canadian, australian: Dialect.Australian,
};
const wanted = new Set(kinds);

const chosen = DIALECTS[String(dialect).toLowerCase()];
// Harper's own `isolateEnglish` was measured dropping ordinary English:
// "Dear Prof, I recieve teh document yesterday and will responsd soon."
// produced zero lints with it on and three with it off. The caller blanks
// Hangul-bearing sentences itself (assist/checks.english_only), which is a
// rule we can explain, so this stays off unless asked for.
const ISOLATE = process.env.FG_ISOLATE === '1';
const linter = new LocalLinter(
  chosen === undefined ? { binary: binaryInlined } : { binary: binaryInlined, dialect: chosen },
);
await linter.setup();

const lints = await linter.lint(text, {
  language: 'plaintext',
  isolateEnglish: ISOLATE,
  dedup: true,
});

const out = [];
for (const lint of lints) {
  const kind = lint.lint_kind();
  if (wanted.size && !wanted.has(kind)) continue;
  const span = lint.span();
  out.push({
    start: span.start,
    end: span.end,
    kind,
    evidence: text.slice(span.start, span.end),
    suggestions: lint.suggestions().slice(0, 3).map((s) => s.get_replacement_text?.() ?? ''),
  });
}
process.stdout.write(JSON.stringify(out));
