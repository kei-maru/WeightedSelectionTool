const state = {
      columns: [], rows: [], users: [], results: [],
      idColumn: null, displayColumns: [], latestSessionId: null,
      events: [], eventId: null, userEventId: null,
      defaultEventName: "default", defaultEventEnabled: true,
      savedUsers: [], savedResults: [], sessions: [], savedLatestSessionId: null,
      resultDisplayColumns: [], savedUserDisplayColumns: [],
      selectedSessionId: null, selectedResults: [], selectedResultDisplayColumns: [],
      mode: "linear", modeLabel: "ゆるやか加重",
      specialRules: [], columnValues: {},
      excludedIndices: [],
      historyImport: null, historySyncBatches: [], allowShutdown: true, guestMode: false,
      calculationSummary: "", selectedCalculationSummary: "",
      summary: { total: 0, winners: 0, idReady: false, displayReady: false }
    };
    let activeTab = "select";
    let specialColumn = null;
    let historyRoles = { idColumn: null, joinColumn: null, winColumn: null };
    let historyBusy = false;
    let historyMode = "add";
    let historyMessage = "";
    let roleBusy = false;
    let isShuttingDown = false;
    const $ = (id) => document.getElementById(id);
    function setStatus(text) { $("status").textContent = text || "待機中"; }
    async function loadAuthUser() {
      try {
        const response = await fetch("/api/auth/me", { cache: "no-store" });
        const data = await response.json();
        if (data.guest) {
          $("authUser").textContent = "ゲスト抽選";
          $("authUser").hidden = false;
          $("logoutLink").hidden = false;
          return;
        }
        if (!data.authenticated || !data.user) return;
        const label = data.user.name || `@${data.user.username}`;
        $("authUser").textContent = `${label} (@${data.user.username})`;
        $("authUser").hidden = false;
        $("logoutLink").hidden = false;
      } catch (_error) {
        // The raffle UI remains usable when authentication is disabled locally.
      }
    }
    function setValue(id, value) {
      const el = $(id);
      if (el) el.value = value;
    }
    function shortDate(value) {
      const text = String(value || "");
      if (!text) return "";
      return text.replace("T", " ").slice(0, 16);
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }
    function colClass(col) {
      if (col === state.idColumn) return "idCol";
      if (state.displayColumns.includes(col)) return "displayCol";
      if (state.specialRules.some(rule => rule.column === col)) return "specialCol";
      return "";
    }
    function cellClass(col, row) {
      if (col === state.idColumn) return "idCol";
      if (state.displayColumns.includes(col)) return "displayCol";
      const rule = state.specialRules.find(item => item.column === col);
      if (rule && String(row.raw[col] ?? "") === String(rule.value ?? "")) return "specialCol";
      return "";
    }
    function setTab(tab) {
      activeTab = tab;
      document.querySelectorAll(".tab").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });
      render();
    }
    function updateStats() {
      $("statTotal").textContent = state.summary.total || state.summary.savedUsers || 0;
      $("statId").textContent = state.idColumn ? "済" : "未";
      $("statDisplay").textContent = state.displayColumns.length;
      $("raffleBtn").disabled = !state.rows.length || !state.idColumn;
      $("fileName").textContent = state.csvFile || "CSV / Excel を読み込んでください。";
      $("mode").value = state.mode || $("mode").value;
      $("mode").disabled = state.guestMode;
      if (state.guestMode) $("mode").value = "equal";
      if ($("eventField")) $("eventField").hidden = state.guestMode;
      document.querySelectorAll('.tab[data-tab="events"], .tab[data-tab="users"]').forEach(tab => {
        tab.hidden = state.guestMode;
      });
      if ($("eventSelect")) {
        $("eventSelect").innerHTML = (state.defaultEventEnabled
          ? `<option value="">${escapeHtml(state.defaultEventName)}</option>` : "") + state.events.map(event =>
          `<option value="${event.id}">${escapeHtml(event.name)}</option>`
        ).join("");
        $("eventSelect").value = state.eventId || "";
      }
      document.querySelectorAll(".exitAction").forEach(button => {
        button.style.display = state.allowShutdown ? "" : "none";
      });
    }
    function mergeState(data, nextTab) {
      Object.assign(state, {
        columns: data.columns || [],
        rows: data.rows || [],
        users: data.users || [],
        results: data.results || [],
        idColumn: data.idColumn || null,
        displayColumns: data.displayColumns || [],
        latestSessionId: data.latestSessionId || null,
        events: data.events || [],
        defaultEventName: data.defaultEventName || "default",
        defaultEventEnabled: data.defaultEventEnabled !== false,
        eventId: data.eventId || null,
        userEventId: data.userEventId || null,
        savedUsers: data.savedUsers || [],
        savedResults: data.savedResults || [],
        sessions: data.sessions || [],
        savedLatestSessionId: data.savedLatestSessionId || null,
        resultDisplayColumns: data.resultDisplayColumns || [],
        savedUserDisplayColumns: data.savedUserDisplayColumns || [],
        selectedSessionId: data.latestSessionId || data.savedLatestSessionId || state.selectedSessionId,
        selectedResults: [],
        selectedResultDisplayColumns: [],
        mode: data.mode || state.mode || "linear",
        modeLabel: data.modeLabel || state.modeLabel || "ゆるやか加重",
        specialRules: data.specialRules || [],
        excludedIndices: data.excludedIndices || [],
        historyImport: data.historyImport || null,
        historySyncBatches: data.historySyncBatches || [],
        allowShutdown: data.allowShutdown !== false,
        guestMode: data.guestMode === true,
        columnValues: data.columnValues || {},
        calculationSummary: data.calculationSummary || state.calculationSummary || "",
        selectedCalculationSummary: "",
        csvFile: data.csvFile || "",
        summary: data.summary || { total: 0, winners: 0, idReady: false, displayReady: false }
      });
      updateStats();
      if (nextTab) activeTab = nextTab;
      render();
      setStatus(data.message);
    }
    function renderGuide() {
      if (activeTab === "select") {
        $("guide").style.display = "";
        $("panelTitle").textContent = "列設定";
        $("guide").innerHTML = `<strong>列を選択してください</strong>
          <div>1. 抽選に使うID列を左クリックしてください。選択された列は<span class="blueText">青色</span>になります。</div>
          <div>2. 結果に表示したい列を左クリックしてください。表示列は<span class="greenText">緑色</span>になります。</div>
          <div>3. 未選択の列を右クリックすると、<span class="orangeText">特別条件</span>を設定できます。条件に合う応募者の確率が上がります。</div>
          <div>4. 左端の「除外」をクリックすると、その応募者を抽選から外せます。除外行は<span class="redText">赤色</span>になります。</div>`;
      } else if (activeTab === "events") {
        $("guide").style.display = "";
        $("panelTitle").textContent = "Event編集";
        $("guide").innerHTML = `<strong>Eventを編集</strong>
          <div>Eventを作成・名前変更・削除できます。主画面では、今回の抽選に使うEventを選ぶだけです。</div>`;
      } else if (activeTab === "results") {
        $("guide").style.display = "";
        $("panelTitle").textContent = "抽選結果一覧";
        const sessionId = state.latestSessionId || state.savedLatestSessionId;
        const session = sessionId ? `Session #${sessionId}` : "記録なし";
        $("guide").innerHTML = `<strong>最新の抽選結果</strong>
          <div>${escapeHtml(session)} の当選者を表示します。CSVを読み込んでいなくても保存済み記録を確認できます。</div>`;
      } else {
        $("guide").style.display = "none";
        $("panelTitle").textContent = "ユーザー一覧";
        $("guide").innerHTML = "";
      }
    }
    function table(headers, body, className = "") {
      return `<table class="${className}"><thead><tr>${headers.join("")}</tr></thead><tbody>${body}</tbody></table>`;
    }
    function renderSelection() {
      if (!state.rows.length) return '<div class="empty">CSV / Excel を読み込んでください。</div>';
      const headers = ['<th class="rowActionHead">除外</th>', ...state.columns.map(col => {
        const special = state.specialRules.find(rule => rule.column === col);
        const badge = col === state.idColumn
          ? " [抽選ID]"
            : state.displayColumns.includes(col)
              ? " [表示列]"
              : special
              ? ` [特別: ${special.value} ×${special.multiplier}]`
              : "";
        return `<th class="${colClass(col)}" data-col="${escapeHtml(col)}" title="クリックで列を指定">${escapeHtml(col + badge)}</th>`;
      })];
      const body = state.rows.map(row => `<tr class="${row.excluded ? "excludedRow" : ""}" data-row="${row.index}">
        <td class="rowActionCell" data-row-action="exclude" title="クリックで抽選から除外">${row.excluded ? "除外中" : "除外"}</td>
        ${state.columns.map(col =>
          `<td class="${cellClass(col, row)}" data-col="${escapeHtml(col)}" title="クリックで列を指定">${escapeHtml(row.raw[col])}</td>`
        ).join("")}</tr>`).join("");
      return table(headers, body, "selectable");
    }
    function renderResults() {
      const currentSessionId = state.latestSessionId || state.selectedSessionId || state.savedLatestSessionId;
      const resultRows = state.results.length
        ? state.results
        : state.selectedResults.length
          ? state.selectedResults
          : state.savedResults;
      const displayColumns = state.results.length
        ? state.displayColumns
        : state.selectedResults.length
          ? state.selectedResultDisplayColumns
          : state.resultDisplayColumns;
      const resultTitle = currentSessionId ? `抽選結果: Session #${currentSessionId}` : "抽選結果";
      const calcText = state.results.length
        ? state.calculationSummary
        : state.selectedCalculationSummary || state.calculationSummary;
      const resultTable = resultRows.length
        ? table(
            ["抽選ID", ...displayColumns].map(c => `<th>${escapeHtml(c)}</th>`),
            resultRows.map(row => `<tr class="winner"><td>${escapeHtml(row.drawId)}</td>${
              displayColumns.map(col => `<td>${escapeHtml(row.displayFields[col])}</td>`).join("")
            }</tr>`).join("")
          )
        : '<div class="empty">まだ抽選結果がありません。</div>';

      const sessionHeaders = ["Session", "Event", "CSV", "モード", "抽選数", "日時", "操作"]
        .map(c => `<th>${escapeHtml(c)}</th>`);
      const sessionBody = state.sessions.length
        ? state.sessions.map(session => `<tr class="${session.id === currentSessionId ? "activeSession" : ""}">
            <td>#${escapeHtml(session.id)}</td>
            <td>${escapeHtml(session.event_name || "default")}</td>
            <td>${escapeHtml(session.csv_file)}</td>
            <td>${escapeHtml(session.mode)}</td>
            <td>${escapeHtml(session.draw_count)}</td>
            <td>${escapeHtml(shortDate(session.created_at))}</td>
            <td>
              <button class="tinyButton sessionOpen" type="button" data-session="${escapeHtml(session.id)}">表示</button>
              <button class="tinyButton sessionDelete" type="button" data-session="${escapeHtml(session.id)}">削除</button>
            </td>
          </tr>`).join("")
        : `<tr><td colspan="7">保存済みセッションがありません。</td></tr>`;
      return `<div class="resultStack">
        <div class="resultBlock">
          <div class="sectionTitle"><span>${escapeHtml(resultTitle)}</span><span class="hint">当選者を最上部に表示しています。</span></div>
          <div style="padding:12px 14px; border-bottom:1px solid var(--line); background:#fff;">
            <strong>計算方法</strong>
            <div class="hint" style="margin-top:4px;">${escapeHtml(calcText || "計算情報がありません。")}</div>
          </div>
          ${resultTable}
        </div>
        <div class="resultBlock">
          <div class="sectionTitle"><span>以前の抽選結果</span><span class="hint">抽選後もここから過去の結果を開けます。</span></div>
          ${table(sessionHeaders, sessionBody)}
        </div>
      </div>`;
    }
    function renderEvents() {
      const defaultRow = state.defaultEventEnabled ? `<tr>
            <td>default</td>
            <td>${escapeHtml(state.defaultEventName)}</td>
            <td>最初から用意されているEventです。</td>
            <td>-</td>
            <td>
              <button class="tinyButton eventEdit" type="button" data-event="__default__">編集</button>
              <button class="tinyButton eventDelete" type="button" data-event="__default__"
                ${state.events.length ? "" : "disabled"}>削除</button>
            </td>
          </tr>` : "";
      const rows = defaultRow + (state.events.length
        ? state.events.map(event => `<tr>
            <td>#${escapeHtml(event.id)}</td>
            <td>${escapeHtml(event.name)}</td>
            <td>${escapeHtml(event.description || "")}</td>
            <td>${escapeHtml(event.created_at || "")}</td>
            <td>
              <button class="tinyButton eventEdit" type="button" data-event="${escapeHtml(event.id)}">編集</button>
              <button class="tinyButton eventDelete" type="button" data-event="${escapeHtml(event.id)}">削除</button>
            </td>
          </tr>`).join("")
        : "");
      return `<div class="resultStack">
        <div class="resultBlock">
          <div class="sectionTitle"><span>Event作成 / 編集</span><span class="hint">抽選SessionをEventごとに分けます。</span></div>
          <div class="panelPad">
            <input id="eventEditId" type="hidden">
            <div class="formGrid">
              <div>
                <label>Event名</label>
                <input id="eventEditName" type="text" placeholder="例: 2026 夏 ASMR イベント">
              </div>
              <div>
                <label>メモ</label>
                <input id="eventEditDescription" type="text" placeholder="任意">
              </div>
            </div>
            <div class="modalActions">
              <button class="primary" type="button" id="eventEditSave">追加</button>
              <button class="soft" type="button" id="eventEditReset">入力をクリア</button>
            </div>
          </div>
          ${table(["ID", "Event名", "メモ", "作成日時", "操作"].map(c => `<th>${escapeHtml(c)}</th>`), rows)}
        </div>
      </div>`;
    }
    function renderUsers() {
      const rows = state.savedUsers;
      const displayColumns = state.savedUserDisplayColumns;
      const selector = `<div class="toolbar">
        <span class="hint">表示するEvent</span>
        <select id="userEventSelect">
          <option value="__all__" ${String(state.userEventId || "__all__") === "__all__" ? "selected" : ""}>すべて</option>
          ${state.defaultEventEnabled
            ? `<option value="__default__" ${String(state.userEventId || "") === "__default__" ? "selected" : ""}>${escapeHtml(state.defaultEventName)}</option>`
            : ""}
          ${state.events.map(event => `<option value="${event.id}" ${String(state.userEventId || "") === String(event.id) ? "selected" : ""}>${escapeHtml(event.name)}</option>`).join("")}
        </select>
        <div class="toolbarActions">
          <button id="historySyncOpen" class="toolbarButton" type="button">データ同期</button>
          <button id="userExport" class="toolbarButton" type="button">Excel出力</button>
        </div>
      </div>`;
      if (!rows.length) return selector + '<div class="empty">このEventの保存済みユーザーがありません。抽選を実行すると追加されます。</div>';
      const headers = ["抽選ID", ...displayColumns, "参加回数", "当選回数", "重み", "現在確率"]
        .map(c => `<th>${escapeHtml(c)}</th>`);
      const body = rows.map(row => `<tr class="${row.winner ? "winner" : ""}">
        <td>${escapeHtml(row.drawId)}</td>
        ${displayColumns.map(col => `<td>${escapeHtml(row.displayFields[col])}</td>`).join("")}
        <td>${escapeHtml(row.join_count)}</td>
        <td>${escapeHtml(row.win_count)}</td>
        <td>${escapeHtml(row.weight)}</td>
        <td>${escapeHtml(row.current_probability)}</td>
      </tr>`).join("");
      return selector + table(headers, body);
    }
    function historyColumnClass(col) {
      if (col === historyRoles.idColumn) return "historyIdCol";
      if (col === historyRoles.joinColumn) return "historyJoinCol";
      if (col === historyRoles.winColumn) return "historyWinCol";
      return "";
    }
    function renderHistoryModal() {
      const currentEvent = $("historyEvent").value;
      const currentMode = $("historyMode").value || historyMode;
      const eventOptions = [
        ...(state.defaultEventEnabled
          ? [`<option value="__default__">${escapeHtml(state.defaultEventName)}</option>`]
          : []),
        ...state.events.map(event => `<option value="${event.id}">${escapeHtml(event.name)}</option>`)
      ].join("");
      $("historyEvent").innerHTML = eventOptions;
      const preferredEvent = state.userEventId && state.userEventId !== "__all__"
        ? state.userEventId
        : state.eventId || "__default__";
      $("historyEvent").value = currentEvent || preferredEvent;
      historyMode = currentMode || "add";
      $("historyMode").value = historyMode;

      const imported = state.historyImport;
      if (!imported) {
        $("historyPreview").innerHTML = '<div class="empty">履歴ファイルを選択してください。</div>';
      } else {
        const headers = imported.columns.map(col =>
          `<th data-history-col="${escapeHtml(col)}" class="${historyColumnClass(col)}">${escapeHtml(col)}</th>`);
        const body = imported.rows.map(row => `<tr>${imported.columns.map(col =>
          `<td class="${historyColumnClass(col)}">${escapeHtml(row[col])}</td>`).join("")}</tr>`).join("");
        $("historyPreview").innerHTML = table(headers, body, "historyTable");
      }
      $("historyApply").disabled = !imported
        || !historyRoles.idColumn || !historyRoles.joinColumn || !historyRoles.winColumn;
      const batches = state.historySyncBatches || [];
      $("historyBatches").innerHTML = batches.length
        ? `<strong>同期履歴</strong>${batches.map(batch => `<div class="historyBatch">
            <span>#${batch.id} / ${escapeHtml(batch.event_name)} / ${escapeHtml(batch.filename)} / ${escapeHtml(batch.sync_mode)} / ${escapeHtml(shortDate(batch.created_at))}</span>
            ${batch.undone_at
              ? '<span class="hint">取消済み</span>'
              : `<button class="tinyButton historyRollback" data-batch="${batch.id}" type="button" ${historyBusy ? "disabled" : ""}>元に戻す</button>`}
          </div>`).join("")}`
        : "";
      $("historyApply").textContent = historyBusy ? "同期中..." : "同期を実行";
      $("historyApply").disabled = $("historyApply").disabled || historyBusy;
      $("historyUploadButton").disabled = historyBusy;
      $("historyStatus").textContent = historyMessage || (imported
        ? "ID列・参加回数列・当選回数列をそれぞれ選択してください。"
        : "ファイルを選び、3つの列を順番に指定してください。");
      $("historyStatus").classList.toggle("error", historyMessage.startsWith("エラー:"));
      $("historyStatus").classList.toggle("busy", historyBusy);
    }
    function chooseHistoryColumn(col) {
      if (historyRoles.idColumn === col) historyRoles.idColumn = null;
      else if (historyRoles.joinColumn === col) historyRoles.joinColumn = null;
      else if (historyRoles.winColumn === col) historyRoles.winColumn = null;
      else if (!historyRoles.idColumn) historyRoles.idColumn = col;
      else if (!historyRoles.joinColumn) historyRoles.joinColumn = col;
      else if (!historyRoles.winColumn) historyRoles.winColumn = col;
      renderHistoryModal();
    }
    function openHistoryModal() {
      historyRoles = { idColumn: null, joinColumn: null, winColumn: null };
      historyMode = "add";
      historyMessage = "";
      $("historyMode").value = "add";
      renderHistoryModal();
      $("historyModal").classList.add("open");
    }
    function closeHistoryModal() {
      $("historyModal").classList.remove("open");
    }
    async function rollbackHistory(batchId) {
      if (!confirm(`同期Batch #${batchId} を元に戻しますか？`)) return;
      historyBusy = true;
      historyMessage = `同期Batch #${batchId} を元に戻しています...`;
      renderHistoryModal();
      setStatus(`同期Batch #${batchId} を元に戻しています...`);
      try {
        const data = await postJson("/api/history/rollback", { batchId });
        mergeState(data, "users");
        historyMessage = data.message || `同期Batch #${batchId} を元に戻しました。`;
        renderHistoryModal();
      } catch (err) {
        historyMessage = `エラー: ${err.message}`;
        setStatus(err.message);
      } finally {
        historyBusy = false;
        renderHistoryModal();
      }
    }
    function bindColumnEvents() {
      document.querySelectorAll(".selectable [data-col]").forEach(cell => {
        cell.addEventListener("click", event => {
          event.stopPropagation();
          chooseColumn(cell.dataset.col);
        });
        cell.addEventListener("contextmenu", event => {
          event.preventDefault();
          event.stopPropagation();
          handleColumnContext(cell.dataset.col);
        });
      });
    }
    function bindSessionEvents() {
      document.querySelectorAll(".sessionOpen").forEach(button => {
        button.addEventListener("click", () => loadSession(button.dataset.session));
      });
      document.querySelectorAll(".sessionDelete").forEach(button => {
        button.addEventListener("click", () => deleteSession(button.dataset.session));
      });
    }
    function bindEventEditorEvents() {
      if ($("historySyncOpen")) {
        $("historySyncOpen").addEventListener("click", openHistoryModal);
      }
      if ($("userExport")) {
        $("userExport").addEventListener("click", () => {
          const eventId = $("userEventSelect")?.value || "__all__";
          window.location.href = `/api/export?eventId=${encodeURIComponent(eventId)}`;
        });
      }
      if ($("userEventSelect")) {
        $("userEventSelect").addEventListener("change", async () => {
          try {
            mergeState(await postJson("/api/user-event", { eventId: $("userEventSelect").value }), "users");
          } catch (err) {
            setStatus(err.message);
          }
        });
      }
      if ($("eventEditSave")) {
        $("eventEditSave").addEventListener("click", async () => {
          try {
            const data = await postJson("/api/event/save", {
              eventId: $("eventEditId").value,
              name: $("eventEditName").value,
              description: $("eventEditDescription").value
            });
            mergeState(data, "events");
          } catch (err) {
            setStatus(err.message);
          }
        });
      }
      if ($("eventEditReset")) {
        $("eventEditReset").addEventListener("click", () => {
          setValue("eventEditId", "");
          setValue("eventEditName", "");
          setValue("eventEditDescription", "");
        });
      }
      document.querySelectorAll(".eventEdit").forEach(button => {
        button.addEventListener("click", () => {
          if (button.dataset.event === "__default__") {
            setValue("eventEditId", "__default__");
            setValue("eventEditName", state.defaultEventName);
            setValue("eventEditDescription", "");
            $("eventEditName").focus();
            return;
          }
          const event = state.events.find(item => String(item.id) === String(button.dataset.event));
          if (!event) return;
          setValue("eventEditId", event.id);
          setValue("eventEditName", event.name || "");
          setValue("eventEditDescription", event.description || "");
          $("eventEditName").focus();
        });
      });
      document.querySelectorAll(".eventDelete").forEach(button => {
        button.addEventListener("click", async () => {
          let targetEventId = null;
          if (button.dataset.event === "__default__") {
            const target = state.events.find(item => String(item.id) === String(state.eventId)) || state.events[0];
            if (!target) return;
            if (!confirm(`${state.defaultEventName}を削除しますか？\nユーザー履歴と抽選記録は「${target.name}」へ移動します。`)) return;
            targetEventId = target.id;
          } else if (!confirm(`Event #${button.dataset.event} を削除しますか？`)) return;
          try {
            mergeState(await postJson("/api/event/delete", {
              eventId: button.dataset.event,
              targetEventId
            }), "events");
          } catch (err) {
            setStatus(err.message);
          }
        });
      });
    }
    function bindRowEvents() {
      document.querySelectorAll("[data-row-action='exclude']").forEach(cell => {
        cell.addEventListener("click", event => {
          event.stopPropagation();
          const row = cell.closest("tr[data-row]");
          if (!row) return;
          toggleExclude(row.dataset.row);
        });
      });
    }
    function render() {
      renderGuide();
      document.querySelectorAll(".tab").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === activeTab);
      });
      if (activeTab === "select") $("tableWrap").innerHTML = renderSelection();
      if (activeTab === "events") $("tableWrap").innerHTML = renderEvents();
      if (activeTab === "results") $("tableWrap").innerHTML = renderResults();
      if (activeTab === "users") $("tableWrap").innerHTML = renderUsers();
      bindColumnEvents();
      bindRowEvents();
      bindSessionEvents();
      bindEventEditorEvents();
    }
    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "操作に失敗しました");
      return data;
    }
    async function chooseColumn(col) {
      if (!col || roleBusy) return;
      let idColumn = state.idColumn;
      let displayColumns = [...state.displayColumns];
      if (col === idColumn) {
        idColumn = null;
      } else if (!idColumn) {
        idColumn = col;
        displayColumns = displayColumns.filter(c => c !== col);
      } else if (displayColumns.includes(col)) {
        displayColumns = displayColumns.filter(c => c !== col);
      } else {
        displayColumns.push(col);
      }
      await updateColumnRoles(idColumn, displayColumns);
    }
    async function cancelColumn(col) {
      if (!col || roleBusy) return;
      const idColumn = state.idColumn === col ? null : state.idColumn;
      const displayColumns = state.displayColumns.filter(c => c !== col);
      await updateColumnRoles(idColumn, displayColumns);
    }
    async function updateColumnRoles(idColumn, displayColumns) {
      roleBusy = true;
      setStatus("列設定を更新しています...");
      try {
        mergeState(await postJson("/api/roles", { idColumn, displayColumns }), "select");
      } catch (err) {
        setStatus(`列設定エラー: ${err.message}`);
      } finally {
        roleBusy = false;
      }
    }
    async function handleColumnContext(col) {
      if (!col) return;
      if (col === state.idColumn || state.displayColumns.includes(col)) {
        await cancelColumn(col);
        return;
      }
      if (state.specialRules.some(rule => rule.column === col)) {
        mergeState(await postJson("/api/special", { column: col, action: "clear" }), "select");
        return;
      }
      openSpecialModal(col);
    }
    function openSpecialModal(col) {
      specialColumn = col;
      $("specialColumnText").textContent = `対象列: ${col}`;
      const values = state.columnValues[col] || [];
      $("specialValue").innerHTML = values.length
        ? values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")
        : '<option value="">選択できる値がありません</option>';
      const existing = state.specialRules.find(rule => rule.column === col);
      $("specialMultiplier").value = existing ? existing.multiplier : 2;
      $("specialSave").disabled = !values.length;
      $("specialModal").classList.add("open");
    }
    function closeSpecialModal() {
      specialColumn = null;
      $("specialModal").classList.remove("open");
    }
    async function toggleExclude(index) {
      try {
        const data = await postJson("/api/exclude", { index });
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    }
    async function loadSession(sessionId) {
      try {
        const data = await postJson("/api/session", { sessionId });
        state.selectedSessionId = data.sessionId;
        state.selectedResults = data.results || [];
        state.selectedResultDisplayColumns = data.displayColumns || [];
        state.selectedCalculationSummary = data.calculationSummary || "";
        state.results = [];
        state.latestSessionId = null;
        activeTab = "results";
        render();
        setStatus(data.message);
        $("tableWrap").scrollTop = 0;
      } catch (err) {
        setStatus(err.message);
      }
    }
    async function deleteSession(sessionId) {
      if (!confirm(`Session #${sessionId} を削除しますか？`)) return;
      try {
        const data = await postJson("/api/session/delete", { sessionId });
        mergeState(data, "results");
      } catch (err) {
        setStatus(err.message);
      }
    }
    async function uploadSelectedFile() {
      const fileInput = $("csvFile");
      if (!fileInput.files.length) return;
      setStatus("読み込み中...");
      const form = new FormData();
      form.append("file", fileInput.files[0]);
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!data.ok) { setStatus(data.error); return; }
      fileInput.value = "";
      mergeState(data, "select");
    }
    async function uploadHistoryFile() {
      const fileInput = $("historyFile");
      if (!fileInput.files.length) return;
      const form = new FormData();
      form.append("file", fileInput.files[0]);
      setStatus("履歴ファイルを読み込み中...");
      historyMessage = "履歴ファイルを読み込んでいます...";
      renderHistoryModal();
      try {
        const res = await fetch("/api/history/upload", { method: "POST", body: form });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || "読み込みに失敗しました");
        historyRoles = { idColumn: null, joinColumn: null, winColumn: null };
        mergeState(data, "users");
        historyMessage = `${data.historyImport?.rows?.length || 0}行を読み込みました。3つの列を指定してください。`;
        renderHistoryModal();
      } catch (err) {
        historyMessage = `エラー: ${err.message}`;
        setStatus(err.message);
        renderHistoryModal();
      } finally {
        fileInput.value = "";
      }
    }
    $("uploadForm").addEventListener("submit", event => event.preventDefault());
    $("uploadButton").addEventListener("click", () => $("csvFile").click());
    $("csvFile").addEventListener("change", uploadSelectedFile);
    $("historyUploadButton").addEventListener("click", () => $("historyFile").click());
    $("historyFile").addEventListener("change", uploadHistoryFile);
    $("historyCancel").addEventListener("click", closeHistoryModal);
    $("historyModal").addEventListener("click", event => {
      if (event.target === $("historyModal")) closeHistoryModal();
    });
    $("historyMode").addEventListener("change", () => {
      historyMode = $("historyMode").value;
      historyMessage = historyMode === "overwrite"
        ? "上書き: このEventの現在の一覧を削除し、読み込んだ内容に入れ替えます。"
        : "追加: 読み込んだ回数を、このEventの現在の回数に足します。";
      renderHistoryModal();
    });
    $("historyModal").addEventListener("click", event => {
      const column = event.target.closest("[data-history-col]");
      if (column) {
        event.stopPropagation();
        chooseHistoryColumn(column.dataset.historyCol);
        return;
      }
      const rollback = event.target.closest(".historyRollback");
      if (rollback) {
        event.stopPropagation();
        rollbackHistory(rollback.dataset.batch);
      }
    });
    $("historyApply").addEventListener("click", async () => {
      if (historyBusy) return;
      if (!state.historyImport || !historyRoles.idColumn || !historyRoles.joinColumn || !historyRoles.winColumn) {
        historyMessage = "エラー: ID列・参加回数列・当選回数列をすべて指定してください。";
        renderHistoryModal();
        return;
      }
      historyBusy = true;
      historyMode = $("historyMode").value || "add";
      historyMessage = historyMode === "overwrite" ? "現在の一覧を入れ替えています..." : "履歴を追加しています...";
      renderHistoryModal();
      setStatus("履歴を同期しています...");
      try {
        const data = await postJson("/api/history/apply", {
          eventId: $("historyEvent").value,
          syncMode: historyMode,
          idColumn: historyRoles.idColumn,
          joinColumn: historyRoles.joinColumn,
          winColumn: historyRoles.winColumn
        });
        mergeState(data, "users");
        historyMessage = data.message || "同期が完了しました。";
        closeHistoryModal();
      } catch (err) {
        historyMessage = `エラー: ${err.message}`;
        setStatus(err.message);
      } finally {
        historyBusy = false;
        renderHistoryModal();
      }
    });
    $("eventSelect").addEventListener("change", async () => {
      try {
        mergeState(await postJson("/api/event/select", { eventId: $("eventSelect").value }), activeTab);
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("raffleBtn").addEventListener("click", async () => {
      try {
        const data = await postJson("/api/raffle", {
          drawCount: $("drawCount").value,
          mode: $("mode").value,
          eventId: $("eventSelect") ? $("eventSelect").value : null,
          allowRepeat: $("allowRepeat").checked,
          notes: $("notes").value
        });
        mergeState(data, "results");
        $("tableWrap").scrollTop = 0;
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("mode").addEventListener("change", async () => {
      try {
        const data = await postJson("/api/mode", { mode: $("mode").value });
        mergeState(data, activeTab);
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialSave").addEventListener("click", async () => {
      if (!specialColumn) return;
      try {
        const data = await postJson("/api/special", {
          column: specialColumn,
          value: $("specialValue").value,
          multiplier: $("specialMultiplier").value,
          action: "set"
        });
        closeSpecialModal();
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialClear").addEventListener("click", async () => {
      if (!specialColumn) return;
      try {
        const data = await postJson("/api/special", {
          column: specialColumn,
          action: "clear"
        });
        closeSpecialModal();
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialCancel").addEventListener("click", closeSpecialModal);
    $("specialModal").addEventListener("click", event => {
      if (event.target === $("specialModal")) closeSpecialModal();
    });
    document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
    document.querySelectorAll(".exitAction").forEach(button => {
      button.addEventListener("click", async () => {
        isShuttingDown = true;
        document.querySelectorAll(".exitAction").forEach(btn => btn.disabled = true);
        setStatus("サーバーを終了しています...");
        try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
        document.body.innerHTML = '<div class="empty">ローカルサーバーを終了しました。このタブは閉じて大丈夫です。</div>';
      });
    });
    window.addEventListener("beforeunload", event => {
      if (isShuttingDown || !state.allowShutdown) return;
      event.preventDefault();
      event.returnValue = "ページを閉じるとローカルサーバーを終了します。";
    });
    async function loadInitialState() {
      try {
        const data = await postJson("/api/state", {});
        mergeState(data);
      } catch (err) {
        setStatus(err.message);
        render();
      }
    }
    loadAuthUser();
    loadInitialState();
