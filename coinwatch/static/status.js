(() => {
  const buttons = document.querySelectorAll('[data-scan-button]');
  const label = document.querySelector('[data-scan-status]');
  const light = document.querySelector('[data-status-light]');
  const page = document.body.dataset.page;
  let wasRunning = document.body.dataset.scanRunning === 'true';

  function refreshedUrl() {
    const url = new URL(window.location.href);
    url.searchParams.delete('scan');
    return url.href;
  }

  // A filter or settings form in progress should never be replaced by a scan refresh.
  document.addEventListener('input', (event) => {
    if (event.target.matches('input:not([type="hidden"]), select, textarea')) {
      event.target.form?.setAttribute('data-edited', 'true');
    }
  });
  document.addEventListener('change', (event) => {
    if (event.target.matches('input:not([type="hidden"]), select, textarea')) {
      event.target.form?.setAttribute('data-edited', 'true');
    }
  });

  function showFinishedNotice(hasError) {
    let notice = document.querySelector('[data-scan-notice]');
    if (!notice) {
      notice = document.createElement('div');
      notice.className = 'notice';
      notice.setAttribute('role', 'status');
      notice.dataset.scanNotice = '';
      document.querySelector('.utility-row').insertAdjacentElement('afterend', notice);
    }
    notice.textContent = 'Scan finished. ';
    const refresh = document.createElement('a');
    refresh.href = refreshedUrl();
    refresh.textContent = 'Refresh for updated results.';
    notice.append(refresh);
    if (hasError) {
      notice.append(' Some checks could not complete. ');
      const history = document.createElement('a');
      history.href = '/history';
      history.textContent = 'See History.';
      notice.append(history);
    }
  }

  async function poll() {
    try {
      const response = await fetch('/api/status', { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) return;
      const status = await response.json();
      const running = status.running === true;
      buttons.forEach((button) => { button.disabled = running; });
      light.classList.toggle('busy', running);
      const mode = { coins: 'Scanning coins', dealers: 'Finding dealers', both: 'Scanning coins and dealers' }[status.mode] || 'Scanning';
      label.textContent = running ? `${mode}: ${status.source || status.phase || 'starting'}` : 'Ready';

      if (wasRunning && !running) {
        const active = document.activeElement;
        const editing = document.querySelector('form[data-edited="true"]') ||
          active?.matches('input:not([type="hidden"]), select, textarea');
        if (['listings', 'sources', 'discoveries', 'history', 'wanted'].includes(page) && !editing) {
          window.location.replace(refreshedUrl());
          return;
        }
        showFinishedNotice(Boolean(status.last_error));
      }
      wasRunning = running;
    } catch (_) {
      // A short server restart should not disrupt the page or an in-progress form.
    }
  }

  window.setInterval(poll, 5000);
})();
