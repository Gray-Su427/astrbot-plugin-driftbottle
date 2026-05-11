const bridge = window.AstrBotPluginPage;
const statsEl = document.getElementById("stats");
const bottlesEl = document.getElementById("bottles");
const messageEl = document.getElementById("message");
const countBadge = document.getElementById("countBadge");
const refreshBtn = document.getElementById("refreshBtn");
const poolFilter = document.getElementById("poolFilter");
const statusFilter = document.getElementById("statusFilter");

let state = null;

function showMessage(text, type = "ok") {
  messageEl.textContent = text;
  messageEl.className = `message ${type}`;
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&": return "&";
      case "<": return "<";
      case ">": return ">";
      case '"': return "";
      case "'": return "&#39;";
      default: return char;
    }
  });
}

function showConfirm(message) {
  return new Promise((resolve) => {
    let modal = document.getElementById("confirmModal");
    let textEl = document.getElementById("confirmModalText");
    const okBtn = document.getElementById("confirmOkBtn");
    const cancelBtn = document.getElementById("confirmCancelBtn");
    if (!modal || !textEl || !okBtn || !cancelBtn) {
      try { resolve(window.confirm(message)); } catch (e) { resolve(false); }
      return;
    }
    textEl.textContent = message;
    modal.style.display = "flex";
    function cleanup() {
      modal.style.display = "none";
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
    }
    function onOk() { cleanup(); resolve(true); }
    function onCancel() { cleanup(); resolve(false); }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}

function normalizeApiResponse(payload) {
  if (payload && typeof payload === "object" && "success" in payload) {
    return payload;
  }
  return { success: true, message: "操作成功", data: payload };
}

async function callApi(endpoint, body) {
  const payload = await bridge.apiPost(endpoint, body);
  const result = normalizeApiResponse(payload);
  if (!result.success) {
    throw new Error(result.message || "请求失败");
  }
  if (result.message) {
    showMessage(result.message, "ok");
  }
  return result.data ?? result;
}

async function callApiGet(endpoint) {
  const payload = await bridge.apiGet(endpoint);
  const result = normalizeApiResponse(payload);
  if (!result.success) {
    throw new Error(result.message || "请求失败");
  }
  return result.data ?? result;
}

function renderStats(data) {
  const s = data.stats;
  const pub = s.public;
  const groupNames = Object.keys(s.groups);
  const totalFloating = pub.floating + groupNames.reduce((sum, n) => sum + s.groups[n].floating, 0);
  const totalRecalled = pub.recalled + groupNames.reduce((sum, n) => sum + s.groups[n].recalled, 0);
  const totalAll = pub.total + groupNames.reduce((sum, n) => sum + s.groups[n].total, 0);

  statsEl.innerHTML = [
    { value: String(totalFloating), label: "漂流中" },
    { value: String(totalRecalled), label: "已收回" },
    { value: String(totalAll), label: "总计" },
    { value: String(data.next_no), label: "下一编号" },
  ]
    .map((item) => `<div class="stat"><strong>${escapeText(item.value)}</strong><span>${escapeText(item.label)}</span></div>`)
    .join("");
}

function getFilteredBottles() {
  if (!state) return [];
  const pool = poolFilter.value;
  const status = statusFilter.value;

  let bottles;
  if (pool === "public") {
    bottles = state.public || [];
  } else {
    bottles = state.groups?.[pool] || [];
  }

  if (status === "floating") {
    bottles = bottles.filter((b) => !b.recalled);
  } else if (status === "recalled") {
    bottles = bottles.filter((b) => b.recalled);
  }

  return bottles;
}

function updatePoolOptions() {
  if (!state) return;
  const current = poolFilter.value;
  const groups = state.groups || {};
  const options = ['<option value="public">大群瓶海</option>'];
  for (const name of Object.keys(groups)) {
    options.push(`<option value="${escapeText(name)}">${escapeText(name)}</option>`);
  }
  poolFilter.innerHTML = options.join("");
  if ([...poolFilter.options].some((o) => o.value === current)) {
    poolFilter.value = current;
  }
}

function renderBottles() {
  const bottles = getFilteredBottles();
  countBadge.textContent = `${bottles.length} 条`;
  countBadge.className = "badge ok";

  if (!bottles.length) {
    bottlesEl.innerHTML = '<div class="empty">没有符合条件的漂流瓶</div>';
    return;
  }

  bottlesEl.innerHTML = bottles
    .map((b) => {
      const no = b.no ?? "?";
      const recalled = b.recalled;
      const statusTag = recalled
        ? '<span class="tag recalled">已收回</span>'
        : '<span class="tag floating">漂流中</span>';
      const likeStr = b.likes > 0 ? `❤️ ${b.likes}` : "";
      const senderName = b.sender_name || "某位同学";
      const pools = (b.pools || []).join("、");

      const cancelBtn = recalled
        ? `<button class="success" data-action="cancel-recall" data-id="${escapeText(b.id)}">取消收回</button>`
        : "";

      return `
        <article class="bottle-card" data-id="${escapeText(b.id)}">
          <div class="bottle-top">
            <span class="bottle-no">第 ${escapeText(String(no))} 号</span>
            <div class="bottle-badges">${statusTag}</div>
          </div>
          <div class="bottle-content">${escapeText(b.content)}</div>
          <div class="bottle-meta">
            <span>来自：${escapeText(senderName)}</span>
            <span>ID：${escapeText(b.sender_id || "")}</span>
            <span>时间：${escapeText(b.created_at || "")}</span>
            ${likeStr ? `<span>${escapeText(likeStr)}</span>` : ""}
            <span>池子：${escapeText(pools)}</span>
          </div>
          <div class="bottle-actions">
            ${cancelBtn}
            <button class="danger" data-action="delete" data-id="${escapeText(b.id)}">删除</button>
          </div>
        </article>
      `;
    })
    .join("");

  bottlesEl.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      const bottleId = btn.dataset.id;
      const no = btn.closest(".bottle-card")?.querySelector(".bottle-no")?.textContent || "";

      try {
        if (action === "delete") {
          const confirmed = await showConfirm(`确认删除 ${no} 漂流瓶吗？此操作不可撤销。`);
          if (!confirmed) return;
          showMessage("正在删除...", "warn");
          await callApi("bottle/delete", { bottle_id: bottleId });
        } else if (action === "cancel-recall") {
          const confirmed = await showConfirm(`确认取消 ${no} 的收回状态吗？`);
          if (!confirmed) return;
          showMessage("正在恢复...", "warn");
          await callApi("bottle/recall-cancel", { bottle_id: bottleId });
        }
        await loadState();
      } catch (error) {
        showMessage(error.message || String(error), "err");
      }
    });
  });
}

async function loadState() {
  const data = await callApiGet("state");
  state = data;
  renderStats(data);
  updatePoolOptions();
  renderBottles();
}

poolFilter.addEventListener("change", () => renderBottles());
statusFilter.addEventListener("change", () => renderBottles());
refreshBtn.addEventListener("click", async () => {
  try {
    await loadState();
    showMessage("已刷新", "ok");
  } catch (error) {
    showMessage(error.message || String(error), "err");
  }
});

(async () => {
  try {
    const pageContext = await bridge.ready();
    showMessage(`已连接到 ${pageContext.displayName || pageContext.pluginName}`, "ok");
    await loadState();
  } catch (error) {
    showMessage(error.message || String(error), "err");
  }
})();