/* Reference shell behaviour only. Application API/actions remain in their existing modules. */
(() => {
  const root = document.getElementById('ma-app');
  const trigger = root?.querySelector('.ma-menu');
  const nav = root?.querySelector('#main-navigation');
  if (!trigger || !nav) return;
  const close = (restoreFocus = false) => {
    nav.classList.remove('ma-open');
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) trigger.focus();
  };
  trigger.addEventListener('click', () => {
    const open = trigger.getAttribute('aria-expanded') !== 'true';
    nav.classList.toggle('ma-open', open);
    trigger.setAttribute('aria-expanded', String(open));
  });
  nav.addEventListener('click', (event) => {
    if (event.target.closest('a')) close();
  });
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && trigger.getAttribute('aria-expanded') === 'true') {
      close(true);
    }
  });
  const desktop = window.matchMedia('(min-width: 651px)');
  desktop.addEventListener('change', () => close());
})();
