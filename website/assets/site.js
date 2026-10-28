(() => {
  const root = document.documentElement;
  root.classList.add('js');
  try { const saved = localStorage.getItem('quantfactra-theme'); if (saved === 'light' || saved === 'dark') root.dataset.theme = saved; } catch {}
  const darkPreference = matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : darkPreference.matches;
  const updateThemeButtons = () => document.querySelectorAll('.theme').forEach(button => button.setAttribute('aria-pressed', String(isDark())));
  updateThemeButtons();
  darkPreference.addEventListener('change', updateThemeButtons);
  document.querySelectorAll('.theme').forEach(button => button.addEventListener('click', () => {
    root.dataset.theme = isDark() ? 'light' : 'dark';
    try { localStorage.setItem('quantfactra-theme', root.dataset.theme); } catch {}
    updateThemeButtons();
  }));

  document.querySelectorAll('.menu-toggle').forEach(button => {
    const menu = document.getElementById(button.getAttribute('aria-controls'));
    const setOpen = open => {
      button.setAttribute('aria-expanded', String(open));
      menu.classList.toggle('is-open', open);
      button.querySelector('span').textContent = open ? '−' : '＋';
    };
    button.hidden = false;
    button.addEventListener('click', () => setOpen(button.getAttribute('aria-expanded') !== 'true'));
    menu.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setOpen(false)));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && button.getAttribute('aria-expanded') === 'true') {
        setOpen(false); button.focus();
      }
    });
    matchMedia('(max-width: 640px)').addEventListener('change', () => setOpen(false));
  });

  document.querySelectorAll('[role="tablist"]').forEach(list => {
    const tabs = [...list.querySelectorAll('[data-tab]')];
    const selectTab = tab => tabs.forEach(other => {
      const active = other === tab;
      other.setAttribute('aria-selected', String(active));
      other.tabIndex = active ? 0 : -1;
      document.getElementById(other.getAttribute('aria-controls')).hidden = !active;
    });
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => selectTab(tab));
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault(); selectTab(tabs[next]); tabs[next].focus();
      });
    });
  });

  document.querySelectorAll('[data-copy]').forEach(button => {
    const prior = button.textContent;
    const status = button.closest('.terminal').querySelector('[data-copy-status]');
    const statusPrior = status?.textContent;
    let reset;
    button.addEventListener('click', async () => {
      const pre = button.dataset.copy === 'active' ? document.querySelector('[role="tabpanel"]:not([hidden])') : document.getElementById(button.dataset.copy);
      try {
        await navigator.clipboard.writeText(pre.textContent.trim());
        button.textContent = '已复制';
      } catch {
        const range = document.createRange(); range.selectNodeContents(pre);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        button.textContent = '已选中，请复制';
      }
      if (status) status.textContent = button.textContent;
      clearTimeout(reset);
      reset = setTimeout(() => {
        button.textContent = prior;
        if (status) status.textContent = statusPrior;
      }, 2200);
    });
  });
})();
