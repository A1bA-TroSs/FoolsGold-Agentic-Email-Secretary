/* Turning an email body into something safe to show.

   Four separate jobs, and they are separate on purpose -- each one has its own
   failure mode and its own reason to exist.

   1. INLINE IMAGES. 543 of the 1,274 messages in a real mailbox reference a
      picture as `cid:something@host`. Chromium has never heard of that scheme,
      so every one of them renders as a broken icon. They are rewritten to the
      local backend, which reads the part out of the original file.

   2. REMOTE IMAGES. Blocked until asked for. A remote image in mail is a
      tracking pixel by default: it tells the sender you opened it, roughly
      when, roughly where you were, and that the address is live. Thunderbird
      has blocked them by default for years for exactly these reasons. The
      count is shown so the user can decide, per message.

   3. LINKS. Rewritten to open in the system browser and nowhere else.

   4. EVERYTHING ELSE. Script, iframes, forms, objects, event handlers and
      `javascript:` URLs are removed. The frame is sandboxed too -- this is the
      second lock, not the only one. A published 0-day in another Electron mail
      viewer was exactly this: `<iframe srcDoc={message.content}>` with, in the
      reporter's words, "no sanitization, no meaningful isolation", and the
      message reached the preload bridge through the top window.
*/

const BLOCKED_TAGS = ['script', 'iframe', 'object', 'embed', 'form', 'input',
  'button', 'textarea', 'select', 'link', 'meta', 'base', 'svg', 'math',
  'applet', 'frame', 'frameset', 'portal'];

// `javascript:` with tabs, newlines or entities in the middle is the classic
// filter bypass, so the test is run on a whitespace-stripped, lowercased copy.
const SAFE_LINK = /^(https?:|mailto:)/i;
const isSafeHref = (raw) => SAFE_LINK.test(String(raw || '').replace(/[\s\u0000-\u001f]/g, ''));

export function prepareBody(html, { emailId, apiBase, showRemote = false }) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  let remoteBlocked = 0;

  for (const el of [...doc.querySelectorAll(BLOCKED_TAGS.join(','))]) el.remove();

  // Event handlers, wherever they are. Attribute names are matched rather than
  // enumerated: `onbeforetoggle` and whatever ships next year are both `on*`.
  for (const el of [...doc.querySelectorAll('*')]) {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on')) el.removeAttribute(attr.name);
      if ((name === 'href' || name === 'src' || name === 'action' || name === 'formaction')
          && /^\s*(javascript|vbscript|data):/i.test(attr.value)
          && !(name === 'src' && /^data:image\//i.test(attr.value.trim()))) {
        el.removeAttribute(attr.name);
      }
    }
  }

  for (const img of [...doc.querySelectorAll('img')]) {
    const src = (img.getAttribute('src') || '').trim();
    if (/^cid:/i.test(src)) {
      // RFC 2392: strip the scheme, keep the rest, and let the backend do the
      // angle-bracket and percent-decoding comparison -- one place, not two.
      const cid = src.slice(4).replace(/^<|>$/g, '');
      img.setAttribute('src', `${apiBase}/api/mail/${encodeURIComponent(emailId)}`
        + `/part/${encodeURIComponent(cid)}`);
    } else if (/^https?:/i.test(src)) {
      remoteBlocked += 1;
      if (!showRemote) {
        img.removeAttribute('src');
        img.setAttribute('data-blocked', src);
        img.setAttribute('alt', img.getAttribute('alt') || '');
      }
    }
  }
  // Background images are the other half of the same tracking surface, and the
  // half people forget: a blocked <img> with a live `background-image` on its
  // parent has told the sender exactly the same thing.
  if (!showRemote) {
    for (const el of [...doc.querySelectorAll('[style]')]) {
      const style = el.getAttribute('style') || '';
      if (/url\(\s*['"]?https?:/i.test(style)) {
        remoteBlocked += 1;
        el.setAttribute('style', style.replace(/url\(\s*['"]?https?:[^)]*\)/gi, 'none'));
      }
    }
  }

  for (const a of [...doc.querySelectorAll('a[href]')]) {
    if (!isSafeHref(a.getAttribute('href'))) { a.removeAttribute('href'); continue; }
    // `_blank` is what makes Electron's window-open handler fire, which is
    // where the scheme is checked again before the OS is asked to open it.
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  }

  return { html: doc.body ? doc.body.innerHTML : '', remoteBlocked };
}

/* The document the frame actually loads.

   `img-src` names the backend explicitly. Without it the CSP that keeps
   everything else out would also block the inline images this whole file
   exists to deliver -- a trap another project hit and shipped as a known
   limitation. */
export function frameDocument(body, { apiBase, showRemote }) {
  const imgSrc = showRemote
    ? `img-src ${apiBase} data: https: http:;`
    : `img-src ${apiBase} data:;`;
  return `<!doctype html><html><head>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; ${imgSrc} style-src 'unsafe-inline'; font-src data:;">
<base target="_blank">
<style>
  html,body{margin:0;padding:6px 2px}
  body{font:14px/1.55 -apple-system,Segoe UI,sans-serif;color:#222;
       word-wrap:break-word;overflow-wrap:anywhere}
  img{max-width:100%;height:auto}
  img[data-blocked]{min-width:14px;min-height:14px;border:1px dashed #bbb;
                    border-radius:3px;background:#f6f6f6}
  table{max-width:100%}
  a{color:#0b62c4}
</style></head><body>${body}</body></html>`;
}
