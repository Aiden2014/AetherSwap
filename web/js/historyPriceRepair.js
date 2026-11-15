let historyPriceRepairPreview = null;
let historyPriceRepairItems = [];

function hprFormatMoney(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "—";
}

function hprFormatTime(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "时间未解析";
  const date = new Date(number * 1000);
  if (Number.isNaN(date.getTime())) return "时间未解析";
  return date.getFullYear() + "-" +
    String(date.getMonth() + 1).padStart(2, "0") + "-" +
    String(date.getDate()).padStart(2, "0") + " " +
    String(date.getHours()).padStart(2, "0") + ":" +
    String(date.getMinutes()).padStart(2, "0");
}

function hprSetStatus(message, isError = false) {
  const status = el("history-price-repair-status");
  if (!status) return;
  status.textContent = message || "";
  status.classList.toggle("text-bad", !!isError);
}

function hprRenderSummary(stats) {
  const summary = el("history-price-repair-summary");
  if (!summary) return;
  stats = stats || {};
  summary.innerHTML = [
    '<span class="summary-stat"><span class="summary-label">Steam Sold 记录</span><span class="summary-value mono">' + Number(stats.sold_rows || 0) + '</span></span>',
    '<span class="summary-stat"><span class="summary-label">命中上架锚点</span><span class="summary-value mono">' + Number(stats.listing_matches || 0) + '</span></span>',
    '<span class="summary-stat"><span class="summary-label">汇率校准样本</span><span class="summary-value mono">' + Number(stats.calibration_anchors || 0) + '</span></span>',
    '<span class="summary-stat"><span class="summary-label">待处理候选</span><span class="summary-value mono">' + historyPriceRepairItems.length + '</span></span>',
    '<span class="summary-stat"><span class="summary-label">存在歧义的操作记录</span><span class="summary-value mono">' + Number(stats.ambiguous_purchases || 0) + '</span></span>'
  ].join("");
}

function hprRenderItems() {
  const body = document.querySelector("#history-price-repair-table tbody");
  if (!body) return;
  if (!historyPriceRepairItems.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted">暂无可校准记录。可能是没有已售记录，或没有足够的 AssetID / 汇率锚点证据。</td></tr>';
    return;
  }
  body.innerHTML = historyPriceRepairItems.map(function (item) {
    const checked = item.can_apply ? "" : " disabled";
    const checkbox = '<input type="checkbox" class="hpr-select" data-purchase-id="' + Number(item.purchase_id) + '"' + checked + ' />';
    const confidenceClass = item.confidence === "高" ? "text-ok" : item.confidence === "低" ? "text-bad" : "";
    const oldPrice = item.current_sale_price == null ? "空" : hprFormatMoney(item.current_sale_price);
    const anchor = item.listing_price_cny == null ? "—" : hprFormatMoney(item.listing_price_cny);
    const fx = item.implied_rate == null ? "—" : Number(item.implied_rate).toFixed(6);
    const title = item.can_apply ? "可选中并应用" : "低置信度：仅供检查，不允许写入";
    return '<tr>' +
      '<td title="' + escapeHtml(title) + '">' + checkbox + '</td>' +
      '<td>' + escapeHtml(item.name || "未知物品") + '<div class="muted small mono">' + escapeHtml(item.assetid || "") + '</div></td>' +
      '<td class="mono">' + oldPrice + '</td>' +
      '<td class="mono">' + hprFormatMoney(item.suggested_sale_price) + '<div class="muted small">' + (Number(item.delta) >= 0 ? "+" : "") + hprFormatMoney(item.delta) + '</div></td>' +
      '<td class="mono">' + escapeHtml(item.raw_price || "—") + '<div class="muted small">' + escapeHtml(item.currency_code || "") + '</div></td>' +
      '<td>' + escapeHtml(item.sold_at_label || hprFormatTime(item.sold_at)) + '</td>' +
      '<td class="mono">' + anchor + '<div class="muted small">' + escapeHtml(item.listing_at_label || "无匹配上架记录") + '</div></td>' +
      '<td class="mono">' + fx + '<div class="muted small">' + Number(item.calibration_samples || 0) + ' 个样本</div></td>' +
      '<td class="' + confidenceClass + '">' + escapeHtml(item.confidence || "低") + '</td>' +
      '<td class="small">' + escapeHtml(item.evidence || "") + '</td>' +
    '</tr>';
  }).join("");
  body.querySelectorAll(".hpr-select").forEach(function (checkbox) {
    checkbox.addEventListener("change", hprUpdateSelection);
  });
  hprUpdateSelection();
}

function hprUpdateSelection() {
  const checkboxes = Array.from(document.querySelectorAll(".hpr-select:not(:disabled)"));
  const selected = checkboxes.filter(function (checkbox) { return checkbox.checked; });
  const count = el("history-price-repair-selected-count");
  if (count) count.textContent = "已选择 " + selected.length + " / " + checkboxes.length + " 条可应用记录";
  const selectAll = el("history-price-repair-select-all");
  if (selectAll) {
    selectAll.checked = checkboxes.length > 0 && selected.length === checkboxes.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < checkboxes.length;
  }
  const applyButton = el("history-price-repair-apply");
  if (applyButton) applyButton.disabled = selected.length === 0;
}

async function loadHistoryPriceRepairAudit() {
  const container = el("history-price-repair-audit");
  if (!container) return;
  try {
    const result = await fetchJson(API + "/history_price_repair/audit");
    if (!result.ok || !Array.isArray(result.batches) || !result.batches.length) {
      container.innerHTML = '<div class="muted small">暂无修复批次记录。</div>';
      return;
    }
    container.innerHTML = '<div class="table-container"><table><thead><tr><th>修复时间</th><th>条数</th><th>备份文件</th><th>状态</th><th>操作</th></tr></thead><tbody>' +
      result.batches.map(function (batch) {
        const rolledBack = Number(batch.rolled_back_count || 0) >= Number(batch.count || 0);
        const status = rolledBack ? "已回滚" : Number(batch.rolled_back_count || 0) > 0 ? "部分回滚" : "已应用";
        const action = rolledBack ? "—" : '<button type="button" class="btn btn-sm btn-danger-outline hpr-rollback" data-batch-id="' + escapeHtml(batch.batch_id) + '">整批回滚</button>';
        const details = (batch.items || []).map(function (item) {
          const meta = item.metadata || {};
          return '<div class="small">' +
            escapeHtml(meta.name || ("操作记录 #" + item.purchase_id)) +
            '：' + hprFormatMoney(item.old_sale_price) + ' → ' + hprFormatMoney(item.new_sale_price) +
            '；' + escapeHtml(meta.currency_code || "") +
            '；' + escapeHtml(meta.evidence || "") +
            (item.rolled_back_at ? ' <span class="muted">（已回滚）</span>' : '') +
            '</div>';
        }).join("");
        return '<tr><td class="mono">' + escapeHtml(hprFormatTime(batch.created_at)) + '</td><td>' + Number(batch.count || 0) + '</td><td class="small mono">' + escapeHtml(batch.backup_path || "—") + '</td><td>' + status + '</td><td>' + action + '<details><summary>查看本批明细</summary>' + details + '</details></td></tr>';
      }).join("") +
      '</tbody></table></div>';
    container.querySelectorAll(".hpr-rollback").forEach(function (button) {
      button.addEventListener("click", async function () {
        const batchId = button.dataset.batchId || "";
        if (!batchId) return;
        const ok = await appConfirm("将把该批次修改过的 sale_price 恢复为修改前数值。若这些记录后来又被其他操作修改，系统会拒绝整批回滚。继续？", {
          title: "回滚历史价格校准",
          danger: true,
          confirmText: "整批回滚"
        });
        if (!ok) return;
        button.disabled = true;
        try {
          const response = await fetchJson(API + "/history_price_repair/rollback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ batch_id: batchId })
          });
          if (!response.ok) throw new Error(response.error || "回滚失败");
          toast("已回滚 " + Number(response.restored || 0) + " 条记录", "备份：" + (response.backup_path || "已创建"));
          await loadHistoryPriceRepairAudit();
          if (typeof refreshTransactions === "function") refreshTransactions();
        } catch (error) {
          toast("回滚失败", error.message || "");
          button.disabled = false;
        }
      });
    });
  } catch (error) {
    container.textContent = "读取修复历史失败：" + (error.message || "");
  }
}

async function initHistoryPriceRepairPanel() {
  await loadHistoryPriceRepairAudit();
}

document.addEventListener("DOMContentLoaded", function () {
  const previewButton = el("history-price-repair-preview");
  const applyButton = el("history-price-repair-apply");
  const selectAll = el("history-price-repair-select-all");

  if (previewButton) {
    previewButton.addEventListener("click", async function () {
      previewButton.disabled = true;
      previewButton.textContent = "正在读取 Steam 历史…";
      hprSetStatus("正在分页读取 Steam Sold 历史并匹配本地上架记录；此步骤不会修改数据库。");
      try {
        const result = await fetchJson(API + "/history_price_repair/preview", { method: "POST" });
        if (!result.ok) throw new Error(result.error || "生成预览失败");
        historyPriceRepairPreview = result.preview_id;
        historyPriceRepairItems = Array.isArray(result.items) ? result.items : [];
        hprRenderSummary(result.stats || {});
        hprRenderItems();
        hprSetStatus(
          "预览已生成，有效期约 30 分钟。只有高/中置信度项目可勾选；低置信度记录仅展示，不会自动写入。"
        );
      } catch (error) {
        historyPriceRepairPreview = null;
        historyPriceRepairItems = [];
        hprRenderItems();
        hprUpdateSelection();
        hprRenderSummary({});
        hprSetStatus("生成预览失败：" + (error.message || ""), true);
      } finally {
        previewButton.disabled = false;
        previewButton.textContent = "读取 Steam 历史并生成预览";
      }
    });
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      document.querySelectorAll(".hpr-select:not(:disabled)").forEach(function (checkbox) {
        checkbox.checked = selectAll.checked;
      });
      hprUpdateSelection();
    });
  }

  if (applyButton) {
    applyButton.disabled = true;
    applyButton.addEventListener("click", async function () {
      const selectedIds = Array.from(document.querySelectorAll(".hpr-select:checked"))
        .map(function (checkbox) { return Number(checkbox.dataset.purchaseId); })
        .filter(function (value) { return Number.isInteger(value) && value > 0; });
      if (!historyPriceRepairPreview || !selectedIds.length) return;
      const ok = await appConfirm(
        "确定应用所选 " + selectedIds.length + " 条成交价格修复？系统会先创建 SQLite 备份，只更新操作记录的 sale_price；数据分析会随之重新计算，上架记录不会被修改。",
        { title: "应用历史价格校准", confirmText: "备份并应用" }
      );
      if (!ok) return;
      applyButton.disabled = true;
      hprSetStatus("正在创建数据库备份并应用所选修复…");
      try {
        const result = await fetchJson(API + "/history_price_repair/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            preview_id: historyPriceRepairPreview,
            purchase_ids: selectedIds
          })
        });
        if (!result.ok) throw new Error(result.error || "应用失败");
        toast("已修复 " + Number(result.updated || 0) + " 条操作记录", "数据库备份：" + (result.backup_path || "已创建"));
        historyPriceRepairPreview = null;
        historyPriceRepairItems = [];
        hprRenderItems();
        hprRenderSummary({});
        hprSetStatus("修复已提交。操作记录与数据分析将在刷新后显示新金额；可在下方修复历史中整批回滚。");
        if (typeof refreshTransactions === "function") await refreshTransactions();
        await loadHistoryPriceRepairAudit();
      } catch (error) {
        hprSetStatus("应用修复失败：" + (error.message || ""), true);
      } finally {
        applyButton.disabled = false;
        hprUpdateSelection();
      }
    });
  }
});
