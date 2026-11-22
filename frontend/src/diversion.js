const diversionLabels = {
  confirmed: ['已确认归属', 'blue'],
  published: ['已发布', 'green'],
  failed: ['发布失败待重试', 'orange'],
};

export function diversionViews({ state, api, shell, esc, fmtDate, toast, refresh }) {
  const slotFor = id => state.slots.find(slot => slot.id === id);
  const sampleFor = id => state.samples.find(sample => sample.id === id);
  const reportFor = id => state.reports.find(report => report.id === id);
  const canPublish = () => ['department_reviewer', 'operations_admin'].includes(state.user.role);

  function distSummary(report) {
    if (!report) return '<span class="muted">—</span>';
    const buckets = report.distribution.filter(row => row.count)
      .map(row => `${esc(row.label.replace(' 分钟', ''))} ${row.count}`).join(' · ') || '无有效样本';
    return `<b>${report.sample_count} 条</b> · P90 ${report.p90_minutes ?? '—'}<div class="muted">${buckets}</div>`;
  }

  function compareRow(slotId, prevId, nextId) {
    const slot = slotFor(slotId);
    return `<tr><td><b>${esc(slot?.department_name)}</b><div class="muted">${esc(slot?.slot_label)}</div></td>
      <td>${distSummary(reportFor(prevId))}</td>
      <td>${nextId ? distSummary(reportFor(nextId)) : '<span class="muted">待发布</span>'}</td></tr>`;
  }

  function card(row) {
    const sample = sampleFor(row.sample_id);
    const [label, color] = diversionLabels[row.status];
    const retry = row.status === 'failed' && canPublish()
      ? `<button class="btn btn-warning" data-diversion-retry="${row.id}">重试发布</button>` : '';
    return `<article class="card operations-card">
      <header class="operations-heading"><div><div class="panel-sub">样本 ${esc(sample?.sample_code)} · ${fmtDate(row.confirmed_at)} 确认</div>
        <h2>${esc(slotFor(row.origin_slot_id)?.department_name)} → ${esc(slotFor(row.admitted_slot_id)?.department_name)}</h2></div>
        <span class="status status-${color}">${label}</span></header>
      <p class="operations-note"><b>分流意见</b> ${esc(row.note)}</p>
      <div class="table-wrap"><table><thead><tr><th>科室时段</th><th>更正前分布</th><th>更正后分布</th></tr></thead>
      <tbody>${compareRow(row.origin_slot_id, row.prev_origin_report_id, row.origin_report_id)}
        ${compareRow(row.admitted_slot_id, row.prev_admitted_report_id, row.admitted_report_id)}</tbody></table></div>
      ${row.published_at ? `<p class="operations-note"><b>发布</b> ${fmtDate(row.published_at)} · 尝试 ${row.publish_attempts} 次 · 旧报告留档可查</p>` : ''}
      ${retry ? `<div class="operations-actions">${retry}</div>` : ''}</article>`;
  }

  function render() {
    const rows = state.diversions;
    return shell(`<section class="operations-workspace"><div class="operations-toolbar"><p>分流到别科的样本沿用原签到叫号，统计改计入接诊科；两科均已归档时，重算报告一起发布，旧报告留档。</p></div>
      <div class="operations-grid">${rows.map(card).join('') || '<div class="card empty">暂无分流转归记录。复核员可在候诊处置台把超时样本分流到别科。</div>'}</div></section>`,
    '分流转归', '跨科分流样本的归属更正，以及两科更正前后的分布对比。');
  }

  function attachEvents() {
    document.querySelectorAll('[data-diversion-retry]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {
        await api(`/diversions/${button.dataset.diversionRetry}/publish`, { method: 'POST' });
        toast('重算报告已发布');
        await refresh();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    });
  }

  return { render, attachEvents };
}
