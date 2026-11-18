(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let jobs = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  let picked = [];
  let jobWm = "";

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t, isErr) {
    msgEl.textContent = t || "";
    msgEl.classList.toggle("err", !!isErr);
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    jobs = (await api("/api/stamp-jobs")).items || [];
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${t.version_no}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function jobBadge(job) {
    if (job.status === "半截") {
      return `<span class="chip chip-half">半截 · 待收口</span>`;
    }
    const cls = job.strategy === "rollback" ? "chip-rollback" : "chip-retain";
    return `<span class="chip ${cls}">已收口 · ${esc(job.strategy_label || job.strategy)}</span>`;
  }

  function renderJobs() {
    if (!jobs.length) return `<p class="muted">暂无盖印作业。勾选多个待盖印运行码可模拟断流半截账。</p>`;
    return jobs
      .map((j) => {
        const rows = (j.batches || [])
          .map(
            (b) => `<tr>
              <td>${esc(b.run_code)}</td>
              <td>${esc(b.table_name)}</td>
              <td><span class="chip">${esc(b.status)}</span></td>
              <td>${esc(b.watermark_value || "-")}</td>
            </tr>`
          )
          .join("");
        const closeCard =
          j.status === "半截"
            ? `<div class="cardish close-card">
                <p class="muted">半截收口只许选一条路，点“继续盖印”后一次做完：</p>
                <div class="row">
                  <button type="button" class="ghost" data-close="${j.id}" data-strategy="rollback">继续盖印：整笔回滚</button>
                  <button type="button" data-close="${j.id}" data-strategy="retain">继续盖印：留存并补盖</button>
                </div>
              </div>`
            : `<p class="muted">收口标记：${esc(j.strategy_label)}（${esc(j.strategy)}）· 收口时间 ${esc(j.closed_at || "-")}</p>`;
        return `<div class="job-card" data-job="${j.id}">
          <div class="job-head">
            <h3>盖印作业 #${j.id} · ${esc(j.watermark_value)}</h3>
            ${jobBadge(j)}
          </div>
          <p class="muted">进度 ${j.progress_text} · 作业态 ${esc(j.status)}</p>
          <table>
            <thead><tr><th>运行码</th><th>表</th><th>批次态</th><th>水印</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${closeCard}
        </div>`;
      })
      .join("");
  }

  function renderBatches() {
    const selectable = batches.filter((b) => b.status === "待盖印" && b.job_id == null);
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账；多选运行码可模拟盖印断流。</p>
      <table>
        <thead><tr><th>选</th><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${
              b.status === "待盖印" && b.job_id == null
                ? `<input type="checkbox" data-pick-batch="${b.id}" ${picked.includes(b.id) ? "checked" : ""}/>`
                : ""
            }</td>
            <td>${b.id}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip">${esc(b.status)}</span></td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${
              b.status === "待盖印" && b.job_id == null
                ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                : b.job_id != null
                ? `<span class="muted">作业#${b.job_id}</span>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <div class="cardish">
        <h3>模拟盖印断流（半截账）</h3>
        <p class="muted">已选 ${picked.length} 个待盖印运行码（至少 2 个），前半截落水印、后半截保持待盖印。</p>
        <label>统一水印值<input id="job-wm-input" value="${esc(jobWm)}" placeholder="例如 WM-BATCH-02" /></label>
        <div class="row">
          <button type="button" id="btn-make-half" ${selectable.length >= 2 ? "" : "disabled"}>制造半截盖印</button>
        </div>
      </div>
      <h2 style="margin-top:1.2rem">盖印卡片（收口标记）</h2>
      ${renderJobs()}
      ${
        selected
          ? `<div class="cardish">
        <h3>单笔盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    const latest = reports[0];
    const openHalf = jobs.filter((j) => j.status === "半截");
    const closed = jobs.filter((j) => j.status === "已收口");
    return `
      <h2>缺口对账 · 复跑台</h2>
      <div class="row"><button type="button" id="btn-gap">复跑对账</button></div>
      <div class="cardish">
        <h3>缺口附页</h3>
        ${
          latest
            ? `<p>最近对账（#${latest.id}，截至 ${esc(latest.as_of_date)}）：应盖印 ${latest.expected_count}，已盖印 ${latest.stamped_count}，缺口 <strong>${latest.gap_count}</strong></p>
               <p class="muted">${esc(latest.summary)}</p>`
            : `<p class="muted">尚未生成对账。</p>`
        }
        <h3>半截作业（缺口是否成立取决于收口取舍）</h3>
        ${
          openHalf.length
            ? `<ul>${openHalf
                .map(
                  (j) => `<li>作业#${j.id} · ${esc(j.watermark_value)} · 进度 ${j.progress_text}
                  <span class="chip chip-half">半截待收口</span>
                  <div class="row">
                    <button type="button" class="ghost" data-close="${j.id}" data-strategy="rollback">整笔回滚</button>
                    <button type="button" data-close="${j.id}" data-strategy="retain">留存补盖</button>
                  </div></li>`
                )
                .join("")}</ul>`
            : `<p class="muted">无半截作业。</p>`
        }
        <h3>已收口留痕</h3>
        ${
          closed.length
            ? `<ul>${closed
                .map(
                  (j) => `<li>作业#${j.id} · ${esc(j.watermark_value)}
                    <span class="chip ${j.strategy === "rollback" ? "chip-rollback" : "chip-retain"}">${esc(j.strategy_label)}</span>
                    <span class="muted">${esc(j.closed_at || "")}</span></li>`
                )
                .join("")}</ul>`
            : `<p class="muted">暂无收口记录。</p>`
        }
      </div>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${r.gap_count}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  async function closeJob(id, strategy) {
    setMsg("");
    try {
      const r = await api(`/api/stamp-jobs/${id}/close`, {
        method: "POST",
        body: JSON.stringify({ strategy }),
      });
      await refresh();
      const label = r.job.strategy_label;
      setMsg(`作业#${id} 已按「${label}」收口完成`);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  function bindCloseButtons() {
    view.querySelectorAll("[data-close]").forEach((btn) => {
      btn.onclick = () => closeJob(Number(btn.dataset.close), btn.dataset.strategy);
    });
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick-batch]").forEach((cb) => {
      cb.onchange = () => {
        const id = Number(cb.dataset.pickBatch);
        if (cb.checked) {
          if (!picked.includes(id)) picked.push(id);
        } else {
          picked = picked.filter((x) => x !== id);
        }
        render();
      };
    });
    const jobWmInput = document.getElementById("job-wm-input");
    if (jobWmInput) {
      jobWmInput.oninput = () => (jobWm = jobWmInput.value);
      document.getElementById("btn-make-half").onclick = async () => {
        setMsg("");
        if (picked.length < 2) {
          setMsg("至少勾选两个待盖印运行码", true);
          return;
        }
        try {
          const r = await api("/api/stamp-jobs", {
            method: "POST",
            body: JSON.stringify({ batch_ids: picked, watermark_value: jobWm }),
          });
          picked = [];
          jobWm = "";
          await refresh();
          setMsg(`已制造半截作业#${r.job.id}：前 ${r.job.stamped_count} 个已落印，其余待盖印`);
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    bindCloseButtons();
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message, true));
})();
