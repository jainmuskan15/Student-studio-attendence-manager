(() => {
  const start = document.getElementById('start-camera');
  const stop = document.getElementById('stop-camera');
  const status = document.getElementById('scan-status');
  const field = document.getElementById('qr-value');
  const form = document.getElementById('scan-form');
  if (!start || !form) return;
  let scanner = null, handled = false;
  start.addEventListener('click', async () => {
    if (typeof Html5Qrcode === 'undefined') {
      status.textContent = 'Scanner library unavailable. Check your internet connection or enter QR data manually.';
      return;
    }
    handled = false;
    try {
      scanner = new Html5Qrcode('qr-reader');
      await scanner.start({ facingMode: 'environment' }, { fps: 10, qrbox: { width: 220, height: 220 } }, value => {
        if (handled) return;
        if (!value.startsWith('SSQR1:')) { status.textContent = 'Not a Student Studio code.'; return; }
        handled = true;
        field.value = value;
        status.textContent = 'Code found. Recording attendance…';
        form.requestSubmit();
      }, () => {});
      start.disabled = true; stop.disabled = false;
      status.textContent = 'Camera ready. Hold the student QR code inside the frame.';
    } catch (err) {
      status.textContent = 'Camera unavailable: ' + (err?.message || String(err));
      scanner = null;
    }
  });
  stop.addEventListener('click', async () => {
    if (!scanner) return;
    try { await scanner.stop(); scanner.clear(); } catch (err) {}
    scanner = null; start.disabled = false; stop.disabled = true;
    status.textContent = 'Camera stopped.';
  });
})();
