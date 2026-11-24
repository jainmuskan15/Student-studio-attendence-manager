(() => {
  const toggle = document.getElementById('theme-toggle');
  toggle?.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('studio-theme', next); } catch (e) {}
  });
  document.getElementById('menu-toggle')?.addEventListener('click', () => document.getElementById('sidebar')?.classList.toggle('open'));
  const modal = document.getElementById('student-modal');
  if (!modal) return;
  let previousFocus;
  const open = () => { previousFocus = document.activeElement; modal.hidden = false; modal.querySelector('input:not([type=hidden])')?.focus(); };
  const close = () => { modal.hidden = true; previousFocus?.focus(); };
  document.querySelectorAll('[data-open-modal]').forEach(el => el.addEventListener('click', open));
  document.querySelectorAll('[data-close-modal]').forEach(el => el.addEventListener('click', close));
  modal.addEventListener('mousedown', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) close(); });
})();
