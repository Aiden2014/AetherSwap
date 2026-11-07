let inventoryHoldingsRows = [];
let inventoryHoldingsBusy = false;
let inventoryHoldingsRequest = 0;

function updateInventorySelection() {
  const checkboxes = [...document.querySelectorAll('#inv-table .inv-select')];
  for (const checkbox of checkboxes) {
    const row = checkbox.closest('tr');
    checkbox.disabled = !row.dataset.assetid;
  }
  const enabled = checkboxes.filter(input => !input.disabled);
  const count = enabled.filter(input => input.checked).length;
  const button = el('btn-inventory-add-holdings');
  if (button) {
    button.disabled = count === 0;
    button.textContent = `添加所选到持有饰品（${count}）`;
  }
  const all = el('inv-select-all');
  if (all) {
    all.checked = enabled.length > 0 && count === enabled.length;
    all.indeterminate = count > 0 && count < enabled.length;
    all.disabled = enabled.length === 0;
  }
}

function inventoryHoldingsSourceLabel(row) {
  if (row.exists) return '已存在，将跳过';
  return { buff: 'BUFF 交易记录', buff_same_price: 'BUFF 同款同价', buff_choices: '选择 BUFF 成交价', buff_selected: '已选 BUFF 成交价' }[row.source] || '手动填写';
}

function renderInventoryHoldingsRows() {
  document.querySelector('#inventory-holdings-table tbody').innerHTML = inventoryHoldingsRows.map((row, index) => `
    <tr><td class="inv-holding-name">${escapeHtml(row.name)}</td><td class="mono inv-holding-assetid" data-label="assetid">${escapeHtml(row.assetid)}</td>
      <td class="inv-holding-price-cell" data-label="购入价（¥ / 件）">
        ${!row.exists && row.price_candidates?.length > 1 ? `<select class="inv-holding-price-choice" data-index="${index}" aria-label="BUFF 成交价">
          <option value="">选择 BUFF 成交价</option>
          ${row.price_candidates.map(choice => `<option value="${Number(choice.price)}" ${row.source === 'buff_selected' && row.price === Number(choice.price) ? 'selected' : ''}>¥${Number(choice.price).toFixed(2)}（${Number(choice.count)} 笔）</option>`).join('')}
        </select>` : ''}
        <input type="number" class="inv-holding-price" data-index="${index}" aria-label="购入价" min="0.01" step="0.01"
        value="${row.price > 0 ? Number(row.price).toFixed(2) : ''}" placeholder="手动填写" ${row.exists ? 'disabled' : ''}></td>
      <td class="inv-holding-source-cell" data-label="价格来源"><span class="inv-holding-source">${inventoryHoldingsSourceLabel(row)}</span></td></tr>
  `).join('');
}

function setInventoryHoldingsBusy(busy) {
  inventoryHoldingsBusy = busy;
  for (const id of ['inventory-holdings-save', 'inventory-holdings-sync', 'inventory-holdings-cancel', 'inventory-holdings-manual']) el(id).disabled = busy;
}

async function syncInventoryHoldingsPrices() {
  if (inventoryHoldingsBusy) return;
  const request = ++inventoryHoldingsRequest;
  setInventoryHoldingsBusy(true);
  el('inventory-holdings-manual').disabled = false;
  el('inventory-holdings-cancel').disabled = false;
  el('inventory-holdings-status').textContent = '正在查询 BUFF 购入记录…';
  try {
    const result = await fetchJson(API + '/inventory/holdings/preview', {
      method: 'POST', body: JSON.stringify({ assetids: inventoryHoldingsRows.map(row => row.assetid) }),
    });
    if (request !== inventoryHoldingsRequest) return;
    if (!result.ok) throw new Error(result.error || '同步失败');
    const previous = new Map(inventoryHoldingsRows.map(row => [row.assetid, row]));
    inventoryHoldingsRows = result.items.map(row => {
      const old = previous.get(row.assetid);
      return old?.manualEdited ? { ...row, price: old.price, source: old.source, manualEdited: true } : row;
    });
    renderInventoryHoldingsRows();
    const needsChoice = inventoryHoldingsRows.filter(row => !row.exists && !row.price && row.price_candidates?.length > 1).length;
    const missing = inventoryHoldingsRows.filter(row => !row.exists && !row.price && !(row.price_candidates?.length > 1)).length;
    const samePrice = inventoryHoldingsRows.filter(row => !row.exists && row.source === 'buff_same_price').length;
    el('inventory-holdings-status').textContent = [result.warning,
      samePrice ? `${samePrice} 件已按查询到的 BUFF 同款同价记录填入，请核对。` : '',
      needsChoice ? `${needsChoice} 件同款存在多个成交价，请从 BUFF 成交价中选择或手动填价。` : '',
      missing ? `${missing} 件未找到唯一匹配的购入记录，请手动填价。` : '',
      !needsChoice && !missing ? '价格已就绪，可修改后添加。' : '',
    ].filter(Boolean).join(' ');
  } catch (error) {
    if (request === inventoryHoldingsRequest) el('inventory-holdings-status').textContent = `${error.message || '同步失败'}；可直接手动填价。`;
  } finally {
    if (request === inventoryHoldingsRequest) setInventoryHoldingsBusy(false);
  }
}

function openInventoryHoldings(rows) {
  if (inventoryHoldingsBusy || !rows.length) return;
  inventoryHoldingsRows = rows.map(row => ({ assetid: row.dataset.assetid, name: row.querySelector('.inv-name').textContent, price: null, source: 'manual' }));
  renderInventoryHoldingsRows();
  el('inventory-holdings-overlay').classList.remove('hidden');
  syncInventoryHoldingsPrices();
}

document.addEventListener('DOMContentLoaded', () => {
  el('inv-table')?.addEventListener('change', updateInventorySelection);
  el('inv-select-all')?.addEventListener('change', event => {
    document.querySelectorAll('#inv-table .inv-select:not(:disabled)').forEach(input => { input.checked = event.target.checked; });
    updateInventorySelection();
  });
  el('btn-inventory-add-holdings')?.addEventListener('click', () => {
    openInventoryHoldings([...document.querySelectorAll('#inv-table .inv-select:checked')].map(input => input.closest('tr')));
  });
  el('inventory-holdings-table')?.addEventListener('input', event => {
    if (!event.target.matches('.inv-holding-price')) return;
    const row = inventoryHoldingsRows[Number(event.target.dataset.index)];
    row.price = event.target.value === '' ? null : Number(event.target.value);
    row.source = 'manual';
    row.manualEdited = true;
    const choice = event.target.closest('tr').querySelector('.inv-holding-price-choice');
    if (choice) choice.value = '';
    event.target.closest('tr').querySelector('.inv-holding-source').textContent = '手动填写';
  });
  el('inventory-holdings-table')?.addEventListener('change', event => {
    if (!event.target.matches('.inv-holding-price-choice') || !event.target.value) return;
    const row = inventoryHoldingsRows[Number(event.target.dataset.index)];
    row.price = Number(event.target.value);
    row.source = 'buff_selected';
    row.manualEdited = true;
    const tr = event.target.closest('tr');
    tr.querySelector('.inv-holding-price').value = row.price.toFixed(2);
    tr.querySelector('.inv-holding-source').textContent = inventoryHoldingsSourceLabel(row);
  });
  el('inventory-holdings-sync')?.addEventListener('click', syncInventoryHoldingsPrices);
  el('inventory-holdings-manual')?.addEventListener('click', () => {
    ++inventoryHoldingsRequest;
    setInventoryHoldingsBusy(false);
    el('inventory-holdings-status').textContent = '请逐件填写购入价（人民币），已有记录会自动跳过。';
  });
  el('inventory-holdings-cancel')?.addEventListener('click', () => {
    ++inventoryHoldingsRequest;
    setInventoryHoldingsBusy(false);
    el('inventory-holdings-overlay').classList.add('hidden');
  });
  el('inventory-holdings-save')?.addEventListener('click', async () => {
    if (inventoryHoldingsBusy) return;
    const invalid = inventoryHoldingsRows.find(row => !row.exists && (!Number.isFinite(row.price) || row.price < 0.01));
    if (invalid) {
      el('inventory-holdings-status').textContent = `请为 ${invalid.name}（${invalid.assetid}）填写有效购入价。`;
      return;
    }
    setInventoryHoldingsBusy(true);
    try {
      const result = await fetchJson(API + '/inventory/holdings', { method: 'POST', body: JSON.stringify({ items: inventoryHoldingsRows.map(row => ({ assetid: row.assetid, price: row.exists ? null : row.price })) }) });
      if (!result.ok) throw new Error(result.error || '添加失败');
      el('inventory-holdings-overlay').classList.add('hidden');
      // The backend remains authoritative; deleted holding records can be added again.
      toast('已添加到持有饰品', `新增 ${result.added.length} 件，跳过重复 ${result.skipped.length} 件`);
      document.querySelectorAll('#inv-table .inv-select:checked').forEach(input => { input.checked = false; });
      updateInventorySelection();
      lastEnrichTime = 0;
      await refreshTransactions();
    } catch (error) {
      el('inventory-holdings-status').textContent = error.message || '添加失败';
    } finally {
      setInventoryHoldingsBusy(false);
    }
  });
});
