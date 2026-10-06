const rectStatusMap = {
  open: ["warn", "待整改"],
  submitted: ["info", "待审核"],
  approved: ["ok", "审核通过"],
  rejected: ["danger", "已驳回"],
  closed: ["muted", "已关闭"],
};

const RectStatusBadge = (s) => {
  const [cls, label] = rectStatusMap[s] || ["muted", s];
  return html`<span class="badge ${cls}">${label}</span>`;
};

const evidenceTypeMap = {
  document: "台账/文件",
  photo: "影像资料",
  data: "数据包",
  other: "其他",
};

views.RectificationsView = () => {
  const user = window.__user;
  const isReg = user.role === "admin" || user.role === "verifier";

  const [orders, setOrders] = React.useState([]);
  const [companies, setCompanies] = React.useState([]);
  const [selYear, setSelYear] = React.useState("");
  const [selStatus, setSelStatus] = React.useState("");
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [detail, setDetail] = React.useState(null);
  const [auditLogs, setAuditLogs] = React.useState(null);
  const [submitting, setSubmitting] = React.useState(false);

  // 开单表单
  const [form, setForm] = React.useState({
    company_id: "", year: 2025, title: "", description: "",
    source_type: "manual", due_date: "",
  });
  // 整改提交表单
  const [submitForm, setSubmitForm] = React.useState({
    measure: "", emission_adjustment: "",
    evidences: [{ evidence_type: "document", name: "", file_url: "", remark: "" }],
  });
  const [reviewDraft, setReviewDraft] = React.useState({
    approved: true, comment: "", confirmed_emission_adjustment: "",
  });

  const load = React.useCallback(async () => {
    const params = new URLSearchParams();
    if (selYear) params.set("year", selYear);
    if (selStatus) params.set("status", selStatus);
    try {
      setOrders(await api.get(`/api/rectifications?${params.toString()}`));
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  }, [selYear, selStatus]);

  React.useEffect(() => {
    if (isReg) api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => { load(); }, [load]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const setS = (k) => (e) => setSubmitForm({ ...submitForm, [k]: e.target.value });

  const refresh = (type, text) => { setMsg({ type, text }); setDetail(null); load(); };

  const years = [2023, 2024, 2025, 2026, 2027];

  // --------------------------------------------------------------- 开单
  const createOrder = async (e) => {
    e.preventDefault();
    if (submitting) return;
    if (!form.company_id) { setMsg({ type: "err", text: "请选择被整改企业" }); return; }
    setSubmitting(true);
    try {
      const r = await api.post("/api/rectifications", {
        company_id: Number(form.company_id),
        year: Number(form.year),
        title: form.title,
        description: form.description,
        source_type: form.source_type,
        due_date: form.due_date,
      }, api.idemKey());
      setForm({ ...form, title: "", description: "", due_date: "" });
      refresh("ok", `整改工单 ${r.order_no} 已开具，状态：待整改`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  // --------------------------------------------------------------- 证据编辑
  const setEv = (i, k, v) => {
    const evs = submitForm.evidences.slice();
    evs[i] = { ...evs[i], [k]: v };
    setSubmitForm({ ...submitForm, evidences: evs });
  };
  const addEv = () => setSubmitForm({
    ...submitForm,
    evidences: [...submitForm.evidences, { evidence_type: "document", name: "", file_url: "", remark: "" }],
  });
  const delEv = (i) => setSubmitForm({
    ...submitForm,
    evidences: submitForm.evidences.filter((_, idx) => idx !== i),
  });

  // 打开详情时把已有整改内容带入表单
  const openDetail = async (o) => {
    try {
      const d = await api.get(`/api/rectifications/${o.id}`);
      setDetail(d);
      setMsg({ type: "", text: "" });
      if (["open", "rejected"].includes(d.status)) {
        setSubmitForm({
          measure: "",
          emission_adjustment: d.emission_adjustment ?? "",
          evidences: [{ evidence_type: "document", name: "", file_url: "", remark: "" }],
        });
      }
      if (d.status === "submitted") {
        setReviewDraft({
          approved: true, comment: "",
          confirmed_emission_adjustment: d.emission_adjustment ?? "",
        });
      }
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  };

  // --------------------------------------------------------------- 提交整改
  const submitRectification = async (o) => {
    const evs = submitForm.evidences.filter((x) => x.name.trim());
    if (submitForm.measure.trim().length < 2) { setMsg({ type: "err", text: "请填写整改措施说明" }); return; }
    if (evs.length === 0) { setMsg({ type: "err", text: "请至少登记一条证据材料" }); return; }
    try {
      await api.post(`/api/rectifications/${o.id}/submit`, {
        rectification_measure: submitForm.measure.trim(),
        emission_adjustment: submitForm.emission_adjustment === "" ? null : Number(submitForm.emission_adjustment),
        evidences: evs.map((x) => ({
          evidence_type: x.evidence_type,
          name: x.name.trim(),
          file_url: x.file_url.trim(),
          remark: x.remark.trim(),
        })),
      }, api.idemKey());
      refresh("ok", `工单 ${o.order_no} 整改已提交，等待核查员审核`);
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  };

  // --------------------------------------------------------------- 审核
  const review = async (o) => {
    if (reviewDraft.comment.trim().length < 2) { setMsg({ type: "err", text: "请填写审核意见（至少 2 字）" }); return; }
    const approved = reviewDraft.approved;
    if (approved || confirm("确认驳回该整改？工单将退回企业重新整改。")) {
      try {
        const r = await api.post(`/api/rectifications/${o.id}/review`, {
          approved,
          comment: reviewDraft.comment.trim(),
          confirmed_emission_adjustment:
            approved && reviewDraft.confirmed_emission_adjustment !== ""
              ? Number(reviewDraft.confirmed_emission_adjustment) : null,
        }, api.idemKey());
        const wb = r.writeback_status ? `，对账回写：${r.writeback_status === "balanced" ? "平衡" : r.writeback_status === "discrepancy" ? "仍有差异" : r.writeback_status}` : "";
        refresh("ok", `工单 ${o.order_no} 已${approved ? "审核通过" : "驳回"}${wb}`);
      } catch (e) {
        setMsg({ type: "err", text: e.message });
      }
    }
  };

  const rerunWriteback = async (o) => {
    try {
      const r = await api.post(`/api/rectifications/${o.id}/rerun-writeback`, {}, api.idemKey());
      refresh("ok", `对账复核完成：${r.writeback_status}（差异 ${r.writeback_discrepancy_count} 项）`);
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  };

  const closeOrder = async (o) => {
    const reason = prompt("关闭原因（至少 2 字，将计入监管审计）：");
    if (reason === null) return;
    if (reason.trim().length < 2) { setMsg({ type: "err", text: "关闭原因至少 2 个字符" }); return; }
    try {
      await api.post(`/api/rectifications/${o.id}/close`, { reason: reason.trim() }, api.idemKey());
      refresh("ok", `工单 ${o.order_no} 已关闭`);
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  };

  const loadAudit = async (o) => {
    try {
      setAuditLogs(await api.get(`/api/rectifications/audit-logs?order_id=${o.id}&limit=200`));
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    }
  };

  const wbBadge = (o) => {
    if (!o.writeback_status) return null;
    if (o.writeback_status === "balanced") return html`<span class="badge ok">对账平衡</span>`;
    if (o.writeback_status === "discrepancy") return html`<span class="badge danger">对账有差异 ${o.writeback_discrepancy_count}</span>`;
    return html`<span class="badge warn">对账${o.writeback_status}</span>`;
  };

  return html`
    ${isReg && html`
    <div class="panel">
      <h3>开具碳排放整改工单</h3>
      <form class="form-grid" onSubmit=${createOrder}>
        <div class="field"><label>被整改企业</label>
          <select value=${form.company_id} onChange=${set("company_id")} required>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${form.year} onChange=${set("year")}>
            ${years.map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>问题来源</label>
          <select value=${form.source_type} onChange=${set("source_type")}>
            <option value="manual">监管巡检开单</option>
            <option value="reconciliation">对账差异</option>
            <option value="report">MRV 报告问题</option>
            <option value="activity">活动数据核验问题</option>
          </select>
        </div>
        <div class="field"><label>整改期限</label>
          <input type="date" value=${form.due_date} onChange=${set("due_date")} />
        </div>
        <div class="field" style=${{gridColumn: "1 / -1"}}><label>问题标题</label>
          <input value=${form.title} onChange=${set("title")} maxlength="200" placeholder="如：外购电力活动量与台账不符" required />
        </div>
        <div class="field" style=${{gridColumn: "1 / -1"}}><label>问题描述与整改要求</label>
          <textarea value=${form.description} onChange=${set("description")} rows="3" required></textarea>
        </div>
        <div class="actions" style=${{gridColumn: "1 / -1"}}>
          <button class="btn" type="submit" disabled=${submitting}>${submitting ? "提交中…" : "开具整改工单"}</button>
        </div>
      </form>
    </div>`}

    <div class="panel">
      <h3>碳排放整改工单</h3>
      <div class="filter-bar">
        <div class="field"><label>年度</label>
          <select value=${selYear} onChange=${(e) => setSelYear(e.target.value)}>
            <option value="">全部</option>
            ${years.map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>状态</label>
          <select value=${selStatus} onChange=${(e) => setSelStatus(e.target.value)}>
            <option value="">全部</option>
            <option value="open">待整改</option>
            <option value="submitted">待审核</option>
            <option value="approved">审核通过</option>
            <option value="rejected">已驳回</option>
            <option value="closed">已关闭</option>
          </select>
        </div>
      </div>
      <table>
        <thead><tr>
          <th>工单号</th>${isReg ? html`<th>企业</th>` : ""}<th>年度</th><th>问题</th><th>来源</th>
          <th>提交轮次</th><th>期限</th><th>状态</th><th>对账回写</th><th></th>
        </tr></thead>
        <tbody>
          ${orders.map((o) => html`
            <tr key=${o.id}>
              <td class="mono">${o.order_no}</td>
              ${isReg ? html`<td>${o.company_name}</td>` : ""}
              <td>${o.year}</td>
              <td style=${{maxWidth: "240px"}}>
                <div style=${{fontWeight: "600"}}>${o.title}</div>
                <div class="muted" style=${{fontSize: "12px"}}>${o.description.slice(0, 40)}${o.description.length > 40 ? "…" : ""}</div>
              </td>
              <td>${o.source_label}</td>
              <td>${o.submit_count}</td>
              <td>${o.due_date || "-"}</td>
              <td>${html([RectStatusBadge(o.status)])}</td>
              <td>${wbBadge(o) || html`<span class="muted">-</span>`}</td>
              <td style=${{whiteSpace: "nowrap"}}>
                <button class="btn ghost sm" onClick=${() => openDetail(o)}>详情</button>
                ${isReg && ["open", "submitted", "rejected"].includes(o.status) && html`
                  <button class="btn danger sm" onClick=${() => closeOrder(o)}>关闭</button>`}
              </td>
            </tr>`)}
          ${orders.length === 0 && html`<tr><td colspan=${isReg ? 10 : 9} class="empty">暂无整改工单</td></tr>`}
        </tbody>
      </table>
    </div>

    ${msg.text && html`<div class="msg ${msg.type === "err" ? "err" : "ok"}">${msg.text}</div>`}

    ${detail && html`
    <div class="panel">
      <h3>工单 ${detail.order_no} 详情
        <button class="btn ghost sm" style=${{float: "right"}} onClick=${() => { setDetail(null); setAuditLogs(null); }}>收起</button>
        ${isReg && html`<button class="btn ghost sm" style=${{float: "right", marginRight: "8px"}} onClick=${() => loadAudit(detail)}>监管审计记录</button>`}
      </h3>
      <div class="cards">
        <div class="card"><div class="label">状态</div><div class="value" style=${{fontSize: "18px"}}>${html([RectStatusBadge(detail.status)])}</div></div>
        <div class="card"><div class="label">问题来源</div><div class="value" style=${{fontSize: "16px"}}>${detail.source_label}</div></div>
        <div class="card"><div class="label">提交轮次</div><div class="value" style=${{fontSize: "18px"}}>${detail.submit_count}</div></div>
        <div class="card"><div class="label">整改期限</div><div class="value" style=${{fontSize: "16px"}}>${detail.due_date || "-"}</div></div>
      </div>
      <p style=${{lineHeight: "1.8"}}><b>${detail.title}</b><br/><span class="muted">${detail.description}</span></p>
      ${detail.review_comment && html`
        <p style=${{marginTop: "8px", padding: "10px 12px", background: "var(--bg-soft)", borderRadius: "8px", fontSize: "13px"}}>
          <b>最近审核意见：</b>${detail.review_comment}
          <span class="muted">（${detail.reviewed_at ? new Date(detail.reviewed_at).toLocaleString("zh-CN") : ""}）</span>
        </p>`}
      ${detail.close_reason && html`
        <p style=${{marginTop: "8px", color: "var(--red)", fontSize: "13px"}}><b>关闭原因：</b>${detail.close_reason}</p>`}

      ${detail.rectification_measure && html`
        <div style=${{marginTop: "12px"}}>
          <h4>企业整改措施${detail.submit_count > 1 ? `（第 ${detail.submit_count} 轮）` : ""}</h4>
          <p style=${{lineHeight: "1.8", fontSize: "13px"}}>${detail.rectification_measure}</p>
          ${detail.emission_adjustment !== null && html`
            <p class="muted" style=${{fontSize: "12px"}}>
              企业申报排放调整：${fmtNum(detail.emission_adjustment, 4)} tCO2e
              ${detail.confirmed_emission_adjustment !== null ? html`；核查认定：<b>${fmtNum(detail.confirmed_emission_adjustment, 4)} tCO2e</b>（已回写履约报告）` : ""}
            </p>`}
        </div>`}

      <div style=${{marginTop: "12px"}}>
        <h4>整改证据（${detail.evidences.length}）</h4>
        ${detail.evidences.length === 0 ? html`<p class="muted" style=${{fontSize: "13px"}}>企业尚未提交证据</p>` : html`
        <table>
          <thead><tr><th>轮次</th><th>类型</th><th>材料名称</th><th>引用/路径</th><th>备注</th><th>提交时间</th></tr></thead>
          <tbody>
            ${detail.evidences.map((ev) => html`
              <tr key=${ev.id}>
                <td>第 ${ev.round} 轮</td>
                <td>${evidenceTypeMap[ev.evidence_type] || ev.evidence_type}</td>
                <td>${ev.file_url ? html`<a href=${ev.file_url} target="_blank">${ev.name}</a>` : ev.name}</td>
                <td class="mono" style=${{fontSize: "12px"}}>${ev.file_url || "-"}</td>
                <td class="muted">${ev.remark || "-"}</td>
                <td>${new Date(ev.created_at).toLocaleString("zh-CN")}</td>
              </tr>`)}
          </tbody>
        </table>`}
      </div>

      ${detail.writeback_status && html`
        <p style=${{marginTop: "12px", fontSize: "13px"}}>
          <b>对账回写：</b>${html([wbBadge(detail)])}
          对账运行 #${detail.writeback_reconciliation_id || "-"} ·
          差异 ${detail.writeback_discrepancy_count ?? "-"} 项
          ${isReg && detail.status === "approved" && html`
            <button class="btn ghost sm" style=${{marginLeft: "10px"}} onClick=${() => rerunWriteback(detail)}>重新对账复核</button>`}
        </p>`}

      ${/* 企业提交整改区 */ ""}
      ${!isReg && ["open", "rejected"].includes(detail.status) && html`
        <div style=${{marginTop: "16px", borderTop: "1px solid var(--line)", paddingTop: "14px"}}>
          <h4>${detail.status === "rejected" ? "按驳回意见重新整改" : "提交整改措施与证据"}</h4>
          <div class="field"><label>整改措施说明</label>
            <textarea rows="3" value=${submitForm.measure} onChange=${setS("measure")}
              placeholder="说明数据核实、更正与重新核算的具体措施"></textarea>
          </div>
          <div class="field" style=${{marginTop: "10px", maxWidth: "280px"}}>
            <label>自查排放调整量（tCO2e，可空；正=补报增排，负=核减）</label>
            <input type="number" step="0.0001" value=${submitForm.emission_adjustment} onChange=${setS("emission_adjustment")} />
          </div>
          <h4 style=${{marginTop: "12px"}}>证据材料
            <button class="btn ghost sm" style=${{marginLeft: "8px"}} onClick=${addEv}>添加一条</button>
          </h4>
          ${submitForm.evidences.map((ev, i) => html`
            <div key=${i} class="form-grid" style=${{marginBottom: "8px", padding: "10px", background: "var(--bg-soft)", borderRadius: "8px"}}>
              <div class="field"><label>类型</label>
                <select value=${ev.evidence_type} onChange=${(e) => setEv(i, "evidence_type", e.target.value)}>
                  <option value="document">台账/文件</option>
                  <option value="photo">影像资料</option>
                  <option value="data">数据包</option>
                  <option value="other">其他</option>
                </select>
              </div>
              <div class="field"><label>材料名称</label>
                <input value=${ev.name} onChange=${(e) => setEv(i, "name", e.target.value)} placeholder="如：电费结算单" /></div>
              <div class="field"><label>文件路径/台账编号</label>
                <input value=${ev.file_url} onChange=${(e) => setEv(i, "file_url", e.target.value)} placeholder="/files/xxx.pdf 或台账编号" /></div>
              <div class="field"><label>备注</label>
                <input value=${ev.remark} onChange=${(e) => setEv(i, "remark", e.target.value)} /></div>
              <div class="field" style=${{justifyContent: "flex-end"}}>
                ${submitForm.evidences.length > 1 && html`<button class="btn danger sm" type="button" onClick=${() => delEv(i)}>删除</button>`}
              </div>
            </div>`)}
          <div class="actions">
            <button class="btn" onClick=${() => submitRectification(detail)}>提交整改（第 ${detail.submit_count + 1} 轮）</button>
          </div>
        </div>`}

      ${/* 核查员审核区 */ ""}
      ${isReg && detail.status === "submitted" && html`
        <div style=${{marginTop: "16px", borderTop: "1px solid var(--line)", paddingTop: "14px"}}>
          <h4>核查审核</h4>
          <div class="field" style=${{maxWidth: "300px"}}><label>审核结论</label>
            <select value=${reviewDraft.approved ? "1" : "0"}
              onChange=${(e) => setReviewDraft({ ...reviewDraft, approved: e.target.value === "1" })}>
              <option value="1">审核通过（回写履约报告并自动对账复核）</option>
              <option value="0">驳回（退回企业重新整改）</option>
            </select>
          </div>
          ${reviewDraft.approved && html`
          <div class="field" style=${{marginTop: "10px", maxWidth: "280px"}}>
            <label>认定排放调整量（tCO2e，可空；缺省取企业申报值）</label>
            <input type="number" step="0.0001" value=${reviewDraft.confirmed_emission_adjustment}
              onChange=${(e) => setReviewDraft({ ...reviewDraft, confirmed_emission_adjustment: e.target.value })} />
          </div>`}
          <div class="field" style=${{marginTop: "10px"}}><label>审核意见</label>
            <textarea rows="2" value=${reviewDraft.comment}
              onChange=${(e) => setReviewDraft({ ...reviewDraft, comment: e.target.value })}
              placeholder=${reviewDraft.approved ? "通过意见" : "驳回原因，将退回企业"}></textarea>
          </div>
          <div class="actions">
            <button class="btn ${reviewDraft.approved ? "" : "danger"}" onClick=${() => review(detail)}>
              ${reviewDraft.approved ? "审核通过" : "驳回整改"}
            </button>
          </div>
        </div>`}
    </div>`}

    ${auditLogs && html`
    <div class="panel">
      <h3>监管审计记录（工单 ${auditLogs.length ? orders.find((o) => o.id === auditLogs[0].order_id)?.order_no || "" : ""}）
        <button class="btn ghost sm" style=${{float: "right"}} onClick=${() => setAuditLogs(null)}>收起</button>
      </h3>
      <table>
        <thead><tr><th>时间</th><th>操作人</th><th>角色</th><th>动作</th><th>结果</th><th>详情</th></tr></thead>
        <tbody>
          ${auditLogs.map((l) => html`
            <tr key=${l.id}>
              <td>${new Date(l.created_at).toLocaleString("zh-CN")}</td>
              <td>${l.operator_name}</td>
              <td>${l.operator_role}</td>
              <td class="mono">${l.action}</td>
              <td>${l.result === "success"
                ? html`<span class="badge ok">成功</span>`
                : html`<span class="badge danger">拒绝</span>`}</td>
              <td style=${{fontSize: "12px"}}>${l.detail}</td>
            </tr>`)}
          ${auditLogs.length === 0 && html`<tr><td colspan="6" class="empty">暂无审计记录</td></tr>`}
        </tbody>
      </table>
    </div>`}
  `;
};
