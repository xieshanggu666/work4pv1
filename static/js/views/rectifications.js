views.RectificationsView = () => {
  const user = window.__user;
  const isReg = user.role === "admin" || user.role === "verifier";
  const [companies, setCompanies] = React.useState([]);
  const [orders, setOrders] = React.useState([]);
  const [selCompany, setSelCompany] = React.useState("");
  const [selStatus, setSelStatus] = React.useState("");
  const [selYear, setSelYear] = React.useState("");
  const [detail, setDetail] = React.useState(null);
  const [showCreate, setShowCreate] = React.useState(false);
  const [showSubmit, setShowSubmit] = React.useState(false);
  const [showEvUpload, setShowEvUpload] = React.useState(false);
  const [auditLogs, setAuditLogs] = React.useState(null);
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  const load = React.useCallback(async () => {
    const params = new URLSearchParams();
    if (isReg && selCompany) params.set("company_id", selCompany);
    if (selStatus) params.set("status", selStatus);
    if (selYear) params.set("year", selYear);
    try {
      setOrders(await api.get(`/api/rectifications?${params.toString()}`));
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  }, [isReg, selCompany, selStatus, selYear]);

  React.useEffect(() => {
    if (isReg) api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const flash = (type, text) => setMsg({ type, text });
  const refreshAll = async (type, text) => {
    await load();
    if (type) flash(type, text);
    if (detail) {
      try {
        const d = await api.get(`/api/rectifications/${detail.id}`);
        setDetail(d);
      } catch (_) {}
    }
  };

  const openDetail = async (o) => {
    setAuditLogs(null);
    try {
      setDetail(await api.get(`/api/rectifications/${o.id}`));
    } catch (e) { flash("err", e.message); }
  };

  const loadAudit = async (o) => {
    try {
      setAuditLogs(await api.get(`/api/rectifications/audit-logs?order_id=${o.id}`));
    } catch (e) { flash("err", e.message); }
  };

  const rejectOrder = async (o) => {
    const reason = prompt("请输入驳回原因（企业将按此重新整改）：", "");
    if (reason === null) return;
    if (reason.trim().length < 2) { flash("err", "驳回原因至少 2 个字符"); return; }
    try {
      await api.post(`/api/rectifications/${o.id}/reject`, { reason });
      await refreshAll("ok", `工单 ${o.order_no} 已驳回，退回企业整改`);
    } catch (e) { flash("err", e.message); }
  };

  const approveOrder = async (o) => {
    const comment = prompt("审核意见（可空）：选择“确定”后审核通过并回写对账差异与履约报告，\n默认同事务重算排放量并发起一次对账复验。", "整改材料齐全，审核通过");
    if (comment === null) return;
    try {
      const r = await api.post(`/api/rectifications/${o.id}/approve`, {
        comment: comment || "", recalculate: true, followup_reconcile: true,
        resolve_discrepancy: "resolved",
      }, api.idemKey());
      const wb = r.writeback || {};
      let text = `工单 ${o.order_no} 已审核通过`;
      if (wb.resolution) text += `，对账差异已标记 ${wb.resolution.status}`;
      if (wb.report_annotated) text += "，整改结论已回写履约报告";
      if (wb.recalculated) text += `，排放量 ${fmtNum(wb.emission_before)} → ${fmtNum(wb.emission_after)} tCO2e`;
      if (r.followup_recon_status) text += `；复验对账：${r.followup_recon_status === "balanced" ? "平衡" : "仍有差异"}`;
      if (wb.discrepancy_still_present) text += "（注意：原差异在复验中仍然存在）";
      await refreshAll("ok", text);
      if (r.warning) flash("warn", r.warning);
    } catch (e) { flash("err", e.message); }
  };

  const closeOrder = async (o) => {
    const reason = prompt("请输入关闭原因（问题不成立/免予整改，关联对账差异将标记为豁免）：", "");
    if (reason === null) return;
    if (reason.trim().length < 2) { flash("err", "关闭原因至少 2 个字符"); return; }
    try {
      await api.post(`/api/rectifications/${o.id}/close`, { reason, resolve_discrepancy: "waived" });
      await refreshAll("ok", `工单 ${o.order_no} 已关闭`);
    } catch (e) { flash("err", e.message); }
  };

  const rectBadge = (s) => {
    const map = {
      open: ["warn", "待整改"], submitted: ["info", "待核查"],
      approved: ["ok", "已通过"], closed: ["muted", "已关闭"],
    };
    const [cls, label] = map[s] || ["muted", s];
    return html`<span class="badge ${cls}">${label}</span>`;
  };
  const evTypeLabel = {
    rectification_plan: "整改方案", rectification_report: "整改报告",
    supporting_doc: "佐证材料", other: "其他",
  };
  const issueTypeLabel = {
    activity_data: "活动数据", emission_factor: "排放因子", calculation: "排放核算",
    report: "MRV报告", reconciliation: "对账差异", quota: "配额", other: "其他",
  };

  const canSubmit = (o) => o.status === "open" && (user.role === "enterprise" || isReg) &&
    (!user.company_id || o.company_id === user.company_id);
  const canUpload = (o) => o.status !== "approved" && o.status !== "closed" &&
    (!user.company_id || o.company_id === user.company_id);

  return html`
    <div class="panel">
      <h3>碳排放整改工单</h3>
      <div class="filter-bar">
        ${isReg && html`
        <div class="field"><label>企业</label>
          <select value=${selCompany} onChange=${(e) => setSelCompany(e.target.value)}>
            <option value="">全部企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>`}
        <div class="field"><label>状态</label>
          <select value=${selStatus} onChange=${(e) => setSelStatus(e.target.value)}>
            <option value="">全部</option>
            <option value="open">待整改</option>
            <option value="submitted">待核查</option>
            <option value="approved">已通过</option>
            <option value="closed">已关闭</option>
          </select>
        </div>
        <div class="field"><label>年度</label>
          <input type="number" value=${selYear} onChange=${(e) => setSelYear(e.target.value)} style=${{width: "90px"}} />
        </div>
        <button class="btn" onClick=${() => setShowCreate(true)}>下发整改工单</button>
      </div>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
      <table>
        <thead><tr>
          <th>工单号</th><th>企业</th><th>年度</th><th>问题</th><th>类型</th><th>来源</th>
          <th>状态</th><th>证据</th><th>期限</th><th></th>
        </tr></thead>
        <tbody>
          ${orders.map((o) => html`
            <tr key=${o.id}>
              <td>${o.order_no}</td>
              <td>${o.company_name}</td>
              <td>${o.year || "-"}</td>
              <td style=${{maxWidth: "220px"}}>${o.title}</td>
              <td>${issueTypeLabel[o.issue_type] || o.issue_type}</td>
              <td>${o.source === "recon_discrepancy" ? html`<span class="badge info">对账转单</span>` : "手工登记"}</td>
              <td>${rectBadge(o.status)}</td>
              <td>${o.evidence_count}</td>
              <td>${o.due_date || "-"}</td>
              <td style=${{whiteSpace: "nowrap"}}>
                <button class="btn ghost sm" onClick=${() => openDetail(o)}>详情</button>
                ${o.status === "submitted" && isReg && html`<button class="btn sm" onClick=${() => approveOrder(o)}>通过</button>`}
                ${o.status === "submitted" && isReg && html`<button class="btn danger sm" onClick=${() => rejectOrder(o)}>驳回</button>`}
                ${(o.status === "open" || o.status === "submitted") && isReg && html`<button class="btn ghost sm" onClick=${() => closeOrder(o)}>关闭</button>`}
              </td>
            </tr>`)}
          ${orders.length === 0 && html`<tr><td colspan="10" class="empty">暂无整改工单</td></tr>`}
        </tbody>
      </table>
    </div>

    ${showCreate && html`
      <${CreateOrderModal} companies=${companies} isReg=${isReg} onClose=${() => setShowCreate(false)}
        onCreated=${async (o) => { setShowCreate(false); await refreshAll("ok", `工单 ${o.order_no} 已创建`); }}
        flash=${flash} />`}

    ${detail && html`
      <div class="panel">
        <h3>工单详情 ${detail.order_no} ${rectBadge(detail.status)}</h3>
        <div class="detail-grid">
          <div><span class="dl">企业：</span>${detail.company_name}（${detail.year || "未指定年度"}）</div>
          <div><span class="dl">问题类型：</span>${issueTypeLabel[detail.issue_type] || detail.issue_type}</div>
          <div><span class="dl">来源：</span>${detail.source === "recon_discrepancy"
            ? `对账差异转单（运行 #${detail.recon_run_id}，代码 ${detail.recon_code}）` : "手工登记"}</div>
          <div><span class="dl">整改期限：</span>${detail.due_date || "-"}</div>
        </div>
        <p><span class="dl">问题描述：</span><br/>${detail.description || "（无）"}</p>
        <p><span class="dl">整改要求：</span><br/>${detail.requirement || "（无）"}</p>
        ${detail.recon_message && html`<p style=${{color: "var(--red)"}}><span class="dl">对账差异：</span><br/>${detail.recon_message}</p>`}
        ${detail.reject_reason && html`<p style=${{color: "var(--red)"}}><span class="dl">最近驳回原因（第 ${detail.rejection_count} 次）：</span><br/>${detail.reject_reason}</p>`}

        ${detail.submission && (detail.submission.summary || detail.evidence_count > 0) && html`
          <div class="subbox">
            <h4>企业整改提交${detail.submitted_at ? `（${new Date(detail.submitted_at).toLocaleString("zh-CN")}）` : ""}</h4>
            <p><span class="dl">整改情况：</span><br/>${detail.submission.summary || "（无）"}</p>
            <p><span class="dl">整改措施：</span><br/>${detail.submission.measures || "（无）"}</p>
            <p><span class="dl">数据/排放影响：</span><br/>${detail.submission.impact || "（无）"}</p>
          </div>`}

        <h4>证据材料（${detail.evidences ? detail.evidences.length : 0}）</h4>
        <table>
          <thead><tr><th>类型</th><th>文件名</th><th>说明</th><th>哈希</th><th>上传时间</th></tr></thead>
          <tbody>
            ${(detail.evidences || []).map((ev) => html`
              <tr key=${ev.id}>
                <td>${evTypeLabel[ev.evidence_type] || ev.evidence_type}</td>
                <td>${ev.file_url ? html`<a href=${ev.file_url} target="_blank">${ev.file_name}</a>` : ev.file_name}</td>
                <td>${ev.description || "-"}</td>
                <td style=${{fontFamily: "monospace", fontSize: "11px"}}>${ev.file_hash ? ev.file_hash.slice(0, 16) + "…" : "-"}</td>
                <td>${new Date(ev.created_at).toLocaleString("zh-CN")}</td>
              </tr>`)}
            ${(!detail.evidences || detail.evidences.length === 0) && html`<tr><td colspan="5" class="empty">暂无证据</td></tr>`}
          </tbody>
        </table>
        <div style=${{marginTop: "10px", display: "flex", gap: "8px", flexWrap: "wrap"}}>
          ${canUpload(detail) && html`<button class="btn" onClick=${() => setShowEvUpload(true)}>上传证据</button>`}
          ${canSubmit(detail) && html`<button class="btn" onClick=${() => setShowSubmit(true)}
            disabled=${detail.evidence_count === 0}>提交整改${detail.evidence_count === 0 ? "（需先上传证据）" : ""}</button>`}
          ${detail.status === "submitted" && isReg && html`<button class="btn" onClick=${() => approveOrder(detail)}>审核通过</button>`}
          ${detail.status === "submitted" && isReg && html`<button class="btn danger" onClick=${() => rejectOrder(detail)}>驳回</button>`}
          ${(detail.status === "open" || detail.status === "submitted") && isReg && html`<button class="btn ghost" onClick=${() => closeOrder(detail)}>关闭工单</button>`}
          ${isReg && html`<button class="btn ghost" onClick=${() => loadAudit(detail)}>监管审计记录</button>`}
        </div>

        ${detail.review_comment && html`<p style=${{marginTop: "10px"}}><span class="dl">审核意见：</span>${detail.review_comment}</p>`}
        ${detail.close_reason && html`<p style=${{color: "var(--text-dim)"}}><span class="dl">关闭原因：</span>${detail.close_reason}</p>`}
        ${detail.corrective_action && Object.keys(detail.corrective_action).length > 0 && html`
          <pre style=${{background: "var(--bg-soft)", border: "1px solid var(--line)", borderRadius: "8px", padding: "12px", marginTop: "10px", fontSize: "12px", overflow: "auto"}}>${JSON.stringify(detail.corrective_action, null, 2)}</pre>`}

        ${showEvUpload && html`<${EvidenceModal} order=${detail} onClose=${() => setShowEvUpload(false)}
          onDone=${async () => { setShowEvUpload(false); await refreshAll("ok", "证据已上传"); }} flash=${flash} />`}
        ${showSubmit && html`<${SubmitModal} order=${detail} onClose=${() => setShowSubmit(false)}
          onDone=${async () => { setShowSubmit(false); await refreshAll("ok", "整改已提交，等待核查员审核"); }} flash=${flash} />`}

        ${auditLogs && html`
          <div class="subbox" style=${{marginTop: "12px"}}>
            <h4>监管审计记录（${auditLogs.length}） <button class="btn ghost sm" onClick=${() => setAuditLogs(null)}>收起</button></h4>
            <table>
              <thead><tr><th>时间</th><th>操作人</th><th>角色</th><th>动作</th><th>结果</th><th>详情</th></tr></thead>
              <tbody>
                ${auditLogs.map((l) => html`
                  <tr key=${l.id}>
                    <td>${new Date(l.created_at).toLocaleString("zh-CN")}</td>
                    <td>${l.operator_name}</td>
                    <td>${l.operator_role}</td>
                    <td>${l.action}</td>
                    <td>${l.result === "success" ? html`<span class="badge ok">成功</span>` : html`<span class="badge danger">拒绝</span>`}</td>
                    <td>${l.detail}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}
      </div>`}
  `;
};

views.CreateOrderModal = ({ companies, isReg, onClose, onCreated, flash }) => {
  const user = window.__user;
  const [form, setForm] = React.useState({
    company_id: user.company_id || "", year: "2026", title: "", issue_type: "other",
    description: "", requirement: "", due_date: "",
  });
  const [saving, setSaving] = React.useState(false);
  const [runId, setRunId] = React.useState("");
  const [discrepancies, setDiscrepancies] = React.useState(null);
  const [pickedIndex, setPickedIndex] = React.useState(null);
  const set = (k) => (e) => {
    setPickedIndex(null);
    setForm({ ...form, [k]: e.target.value });
  };

  const loadRun = async () => {
    const id = Number(runId);
    if (!id) { flash("err", "请输入对账运行编号（#id）"); return; }
    try {
      const run = await api.get(`/api/ledger/reconciliations/${id}`);
      if (run.status !== "discrepancy") flash("warn", `运行 ${run.recon_no} 结论为 ${run.status}，没有差异`);
      setDiscrepancies(run.discrepancies || []);
      setPickedIndex(null);
    } catch (e) { flash("err", e.message); }
  };

  const pickDiscrepancy = (idx, d) => {
    setPickedIndex(idx);
    const refs = d.refs || {};
    setForm((f) => ({
      ...f,
      company_id: refs.company_id || (runId ? f.company_id : f.company_id),
      year: refs.year != null ? String(refs.year) : f.year,
      title: `对账差异整改：${d.code}`,
      issue_type: "reconciliation",
      description: d.message || "",
    }));
  };

  const save = async () => {
    if (!form.company_id) { flash("err", "请选择企业"); return; }
    if (form.title.trim().length < 2) { flash("err", "问题标题至少 2 个字符"); return; }
    if (pickedIndex === null && discrepancies && discrepancies.length > 0) {
      flash("err", "已载入差异列表，请点击“选择此差异转单”，或清空对账运行编号改手工建单");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        ...form,
        company_id: Number(form.company_id),
        year: form.year ? Number(form.year) : null,
      };
      if (pickedIndex !== null) {
        payload.recon_run_id = Number(runId);
        payload.recon_discrepancy_index = pickedIndex;
      }
      const o = await api.post("/api/rectifications", payload, api.idemKey());
      onCreated(o);
    } catch (e) { flash("err", e.message); } finally { setSaving(false); }
  };

  return html`
    <div class="modal-mask" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()} style=${{maxWidth: "620px"}}>
        <h3>下发碳排放整改工单</h3>
        ${isReg && html`
          <div class="subbox">
            <div class="field"><label>从对账差异转单（可选）：输入对账运行 #id 载入差异</label>
              <div style=${{display: "flex", gap: "8px"}}>
                <input value=${runId} onChange=${(e) => setRunId(e.target.value)} placeholder="如 12" style=${{flex: 1}} />
                <button type="button" class="btn ghost" onClick=${loadRun}>载入差异</button>
              </div>
            </div>
            ${discrepancies !== null && html`
              <div style=${{maxHeight: "160px", overflowY: "auto", marginTop: "8px"}}>
                ${discrepancies.length === 0 && html`<div class="empty" style=${{padding: "10px"}}>该运行无差异</div>`}
                ${discrepancies.map((d, idx) => html`
                  <div key=${idx} style=${{borderBottom: "1px solid var(--line)", padding: "6px 2px", fontSize: "12px"}}>
                    <div><b>${d.code}</b> <span class="badge ${d.severity === "error" ? "danger" : "warn"}">${d.severity}</span>
                      ${d.resolution && html`<span class="badge muted">已${d.resolution.status === "resolved" ? "整改" : "豁免"}但复发</span>`}</div>
                    <div style=${{color: "var(--text-dim)"}}>${(d.message || "").slice(0, 90)}</div>
                    <button type="button" class="btn sm ${pickedIndex === idx ? "" : "ghost"}"
                      onClick=${() => pickDiscrepancy(idx, d)}>
                      ${pickedIndex === idx ? "已选择" : "选择此差异转单"}</button>
                  </div>`)}
              </div>`}
          </div>`}
        <div class="field" style=${{marginTop: "10px"}}><label>企业 *</label>
          ${isReg ? html`
          <select value=${form.company_id} onChange=${set("company_id")}>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>` : html`<input value=${user.display_name || user.username} disabled />`}
        </div>
        <div class="field"><label>年度</label><input type="number" value=${form.year} onChange=${set("year")} /></div>
        <div class="field"><label>问题标题 *</label><input value=${form.title} onChange=${set("title")} placeholder="如：2026年度燃煤活动数据缺计量佐证" /></div>
        <div class="field"><label>问题类型</label>
          <select value=${form.issue_type} onChange=${set("issue_type")}>
            <option value="activity_data">活动数据</option>
            <option value="emission_factor">排放因子</option>
            <option value="calculation">排放核算</option>
            <option value="report">MRV报告</option>
            <option value="reconciliation">对账差异</option>
            <option value="quota">配额</option>
            <option value="other">其他</option>
          </select>
        </div>
        <div class="field"><label>问题描述</label><textarea rows="3" value=${form.description} onChange=${set("description")} /></div>
        <div class="field"><label>整改要求</label><textarea rows="2" value=${form.requirement} onChange=${set("requirement")} /></div>
        <div class="field"><label>整改期限</label><input type="date" value=${form.due_date} onChange=${set("due_date")} /></div>
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>取消</button>
          <button class="btn" disabled=${saving} onClick=${save}>${saving ? "提交中..." : "创建工单"}</button>
        </div>
      </div>
    </div>`;
};

views.EvidenceModal = ({ order, onClose, onDone, flash }) => {
  const [form, setForm] = React.useState({
    evidence_type: "supporting_doc", file_name: "", file_url: "",
    file_hash: "", file_size: 0, description: "",
  });
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const onFile = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      let hash = "";
      try {
        const buf = await crypto.subtle.digest("SHA-256", reader.result);
        hash = Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
      } catch (_) {}
      setForm((f) => ({ ...f, file_name: file.name, file_size: file.size, file_hash: hash }));
    };
    reader.readAsArrayBuffer(file);
  };

  const save = async () => {
    if (!form.file_name.trim()) { flash("err", "请选择文件或填写文件名"); return; }
    try {
      await api.post(`/api/rectifications/${order.id}/evidences`, {
        ...form, file_size: Number(form.file_size) || 0,
      });
      onDone();
    } catch (e) { flash("err", e.message); }
  };

  return html`
    <div class="modal-mask" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>上传整改证据 · ${order.order_no}</h3>
        <div class="field"><label>证据类型</label>
          <select value=${form.evidence_type} onChange=${set("evidence_type")}>
            <option value="rectification_plan">整改方案</option>
            <option value="rectification_report">整改报告</option>
            <option value="supporting_doc">佐证材料（台账/票据/照片）</option>
            <option value="other">其他</option>
          </select>
        </div>
        <div class="field"><label>选择文件（本地计算 SHA-256 指纹）</label>
          <input type="file" onChange=${onFile} />
        </div>
        <div class="field"><label>文件名 *</label><input value=${form.file_name} onChange=${set("file_name")} /></div>
        <div class="field"><label>文件链接（可选）</label><input value=${form.file_url} onChange=${set("file_url")} placeholder="https://..." /></div>
        <div class="field"><label>SHA-256</label><input value=${form.file_hash} onChange=${set("file_hash")} placeholder="自动计算，可手填" /></div>
        <div class="field"><label>说明</label><textarea rows="2" value=${form.description} onChange=${set("description")} /></div>
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>取消</button>
          <button class="btn" onClick=${save}>上传</button>
        </div>
      </div>
    </div>`;
};

views.SubmitModal = ({ order, onClose, onDone, flash }) => {
  const [form, setForm] = React.useState({ summary: "", measures: "", impact: "" });
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const save = async () => {
    if (form.summary.trim().length < 2) { flash("err", "整改情况说明至少 2 个字符"); return; }
    try {
      await api.post(`/api/rectifications/${order.id}/submit`, form, api.idemKey());
      onDone();
    } catch (e) { flash("err", e.message); }
  };
  return html`
    <div class="modal-mask" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>提交整改 · ${order.order_no}</h3>
        <div class="field"><label>整改情况说明 *</label><textarea rows="3" value=${form.summary} onChange=${set("summary")} /></div>
        <div class="field"><label>整改措施</label><textarea rows="3" value=${form.measures} onChange=${set("measures")} /></div>
        <div class="field"><label>对排放数据/履约的影响</label><textarea rows="2" value=${form.impact} onChange=${set("impact")} /></div>
        <p style=${{color: "var(--text-dim)", fontSize: "12px"}}>提交后工单进入待核查状态，核查员可审核通过、驳回重改或关闭。</p>
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>取消</button>
          <button class="btn" onClick=${save}>提交整改</button>
        </div>
      </div>
    </div>`;
};
