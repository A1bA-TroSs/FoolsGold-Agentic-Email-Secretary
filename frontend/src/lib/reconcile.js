/* Keep the objects that did not change.

   Every reload replaces the whole list with freshly parsed JSON, so even the
   rows whose every field is identical arrive as new objects. React compares by
   identity, so "nothing changed" and "everything changed" are indistinguishable
   to it, and a reload of a 187-row list re-renders 187 rows and 187 briefing
   entries -- measured as a ~140ms hitch arriving 800ms after a click, which is
   precisely when the completion animation is finishing and the user is still
   watching it.

   This is the missing half of memoising the row. `React.memo` can only skip
   work when the props hold still, and the props are these objects. Ticking one
   email already preserved identity for the others, because the optimistic
   update maps over the array and returns the same object for untouched rows.
   The reload threw that away again a moment later.

   The comparison is shallow, one level deep, with arrays compared
   element-wise: the API returns flat rows whose only non-primitive field is a
   list of matched topics. A nested object is treated as changed, which is the
   safe direction -- a missed reuse costs one render, a false reuse shows stale
   data. */

function sameValue(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }
  // NaN, and nothing else: two different objects are reported as different.
  return a !== a && b !== b;
}

export function sameRow(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  for (const k of ka) {
    if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
    if (!sameValue(a[k], b[k])) return false;
  }
  return true;
}

/* Returns a list that is `next` in content but reuses `prev`'s objects wherever
   a row is unchanged -- and returns `prev` itself when nothing at all moved, so
   the setState is a no-op and React does not even re-render the list. */
export function reuseUnchanged(prev, next, key = 'id') {
  if (!Array.isArray(next)) return next;
  if (!Array.isArray(prev) || prev.length === 0) return next;

  const byKey = new Map();
  for (const row of prev) byKey.set(row?.[key], row);

  let identical = prev.length === next.length;
  const out = next.map((row, i) => {
    const old = byKey.get(row?.[key]);
    if (old && sameRow(old, row)) {
      if (identical && prev[i] !== old) identical = false;
      return old;
    }
    identical = false;
    return row;
  });
  return identical ? prev : out;
}
