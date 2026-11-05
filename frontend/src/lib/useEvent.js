import { useCallback, useLayoutEffect, useRef } from 'react';

/* A callback whose identity never changes but whose body is always current.

   This exists because of a measured stall, not a style preference. Ticking one
   email off a 187-row list produced a 219ms long frame, attributed by the Long
   Animation Frames API to `MessagePort.onmessage` -- React's scheduler -- with
   rendering at 11ms and style+layout at 0ms. Nothing was slow; everything was
   simply being re-rendered. The completion animation then dropped frames for
   the whole of its 800ms, which is precisely when the user is looking at it.

   `React.memo` on the row is the fix, but memo compares props, and half the
   props were fresh closures on every render. `applyFeedback` declared
   `[mail, view, loadMail]` as its dependencies, and `mail` is replaced by the
   very click being animated -- so the handler was reborn exactly when it most
   needed to stay put, and every row re-rendered to receive it. `askMute` and
   `setHighlight` were plain function declarations, reborn on every render of
   any kind.

   The honest alternative -- hoisting state until the dependency arrays go
   empty -- is a larger rewrite of App for a smaller gain. This keeps the
   dependency list correct by making it unnecessary: the ref holds the latest
   closure, the wrapper is created once, and callers see a constant.

   The ref is updated in a layout effect rather than during render, so a render
   that React discards (StrictMode, a suspended tree) cannot leave a stale or
   premature closure behind. Do not call the returned function during render --
   in the first commit the ref still holds the initial closure, and this is for
   event handlers, which by definition run after paint. */
export function useEvent(fn) {
  const ref = useRef(fn);
  useLayoutEffect(() => { ref.current = fn; });
  return useCallback((...args) => ref.current?.(...args), []);
}
