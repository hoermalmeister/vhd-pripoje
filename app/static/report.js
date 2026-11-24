// Tlačítko „Nahlásit chybu“: pošle zadání i odpověď serveru na /api/report (uloží se a zobrazí v administraci).
(function () {
  const css = `.report{margin-top:10px}
  .report .rep-btn{background:transparent;color:var(--warn,#8a5a00);border:1px solid currentColor;border-radius:8px;padding:6px 12px;font:inherit;font-size:.88rem;font-weight:600;cursor:pointer}
  .report textarea{width:100%;min-height:60px;margin-top:6px;padding:8px;border:1px solid var(--line,#ccc);border-radius:8px;font:inherit;font-size:.9rem;background:var(--panel,#fff);color:var(--ink,#000)}
  .report .rep-send{margin-top:6px;background:var(--accent,#0b4f8a);color:var(--accent-ink,#fff);border:0;border-radius:8px;padding:7px 14px;font:inherit;font-size:.9rem;font-weight:600;cursor:pointer}
  .report .rep-msg{font-size:.88rem;margin-top:6px;color:var(--muted,#666)}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  const payloads = {};
  let seq = 0;
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  window.Report = {
    // vrátí HTML tlačítka; payload = {page, input, response}
    html(payload) {
      const id = "r" + (++seq);
      payloads[id] = payload;
      return `<div class="report" data-rid="${id}"><button type="button" class="rep-btn">⚑ Nahlásit chybu</button></div>`;
    }
  };

  document.addEventListener("click", async ev => {
    const box = ev.target.closest(".report");
    if (!box) return;
    const id = box.dataset.rid;
    if (ev.target.classList.contains("rep-btn")) {
      box.innerHTML = `<textarea placeholder="Co je špatně? (nepovinné – např. „vlak ve skutečnosti počkal“)"></textarea>
        <button type="button" class="rep-send">Odeslat hlášení</button><div class="rep-msg">Odešle se zadání a odpověď aplikace, nic dalšího.</div>`;
      box.querySelector("textarea").focus();
    } else if (ev.target.classList.contains("rep-send")) {
      const btn = ev.target, msg = box.querySelector(".rep-msg");
      btn.disabled = true; msg.textContent = "Odesílám…";
      try {
        const body = Object.assign({}, payloads[id], {note: box.querySelector("textarea").value.trim()});
        const r = await fetch("/api/report", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || r.status);
        box.innerHTML = `<div class="rep-msg">Děkuji, hlášení č. ${esc(j.id)} je odesláno.</div>`;
      } catch (e) { msg.textContent = "Nepodařilo se odeslat: " + e.message; btn.disabled = false; }
    }
  });
})();
