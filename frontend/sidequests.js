/* Scheduling is supplied by the server in Asia/Hong_Kong, not the browser timezone. */
const sqState = { quests: [], date: null, editingId: null, busy: false, loading: false,
    queued: false, lastFocus: null };

function sqEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, char =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

async function sqRequest(path = '', options = {}) {
    const response = await fetch('/api/sidequests' + path, {
        ...options, headers: { 'Content-Type': 'application/json', ...options.headers }
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        const detail = data?.detail;
        throw new Error(Array.isArray(detail) ? detail.map(error => error.msg).join('; ')
            : (typeof detail === 'string' ? detail : `Side Quests request failed (${response.status}).`));
    }
    return data;
}

function sqSetBusy(value) {
    sqState.busy = value;
    document.querySelectorAll('.sq-action').forEach(button => { button.disabled = value; });
}

function sqScheduleLabel(quest) {
    return quest.schedule_type === 'Daily' ? 'Every day' : (quest.weekdays || []).join(' ');
}

function sqCard(quest, compact = false, manage = false) {
    const id = Number(quest.id);
    const completion = quest.scheduled_today || quest.completed_today;
    const checkbox = completion ? `<input class="sq-action" type="checkbox"
        aria-label="${quest.completed_today ? 'Undo completion for' : 'Complete'} ${sqEscape(quest.title)}"
        ${quest.completed_today ? 'checked' : ''} onchange="sqToggle(${id}, this)">` : '';
    return `<div class="${compact ? '' : 'task '}${quest.completed_today ? 'sq-done' : ''}">
        <div class="sq-row">${checkbox}<div class="sq-content">
            <h3>${sqEscape(quest.title)}</h3>
            ${!compact && quest.description ? `<p class="sq-description">${sqEscape(quest.description)}</p>` : ''}
            ${!compact ? `<div class="task-meta"><span class="badge">${sqEscape(sqScheduleLabel(quest))}</span>
                ${!quest.active ? '<span class="badge">Inactive</span>' : ''}
                ${manage && !quest.scheduled_today && quest.active ? '<span class="badge">Not scheduled today</span>' : ''}
                ${quest.completed_today ? '<span class="badge">Completed today</span>' : ''}</div>` : ''}
        </div><span class="badge sq-xp">+${quest.completed_today ? quest.earned_xp_today : quest.xp} XP</span></div>
        ${!compact ? `<div class="sq-actions">
            <button class="btn btn-secondary sq-action" onclick="sqOpenModal(${id})">Edit</button>
            <button class="btn btn-secondary sq-action" onclick="sqSetActive(${id}, ${!quest.active})">${quest.active ? 'Deactivate' : 'Reactivate'}</button>
        </div>` : ''}</div>`;
}

async function loadSideQuests() {
    if (sqState.loading) { sqState.queued = true; return; }
    sqState.loading = true;
    try {
        const [today, stats, quests] = await Promise.all([
            sqRequest('/today'), sqRequest('/stats'), sqRequest()
        ]);
        // If midnight falls between responses, refresh instead of mixing local dates.
        if (today.date !== stats.today.date) { sqState.queued = true; return; }
        sqState.quests = quests;
        sqState.date = today.date;
        const progress = `${stats.today.completed_count} / ${stats.today.scheduled_count} completed · ${stats.today.xp} XP`;
        document.getElementById('sqCompactProgress').textContent = progress;
        document.getElementById('sqTodayProgress').textContent = progress;
        document.getElementById('sqLocalDate').textContent = today.date + ' · Hong Kong';
        document.getElementById('sqTodayList').innerHTML = today.quests.length
            ? today.quests.map(quest => sqCard(quest)).join('')
            : '<div class="empty">No Side Quests scheduled today. Create one or manage your schedules below.</div>';
        document.getElementById('sqCompactList').innerHTML = today.quests.slice(0, 4)
            .map(quest => sqCard(quest, true)).join('') + (today.quests.length > 4
                ? `<p class="sq-muted">${today.quests.length - 4} more on the Side Quests page.</p>`
                : (!today.quests.length ? '<div class="empty">No Side Quests scheduled today.</div>' : ''));
        document.getElementById('sqWeekProgress').textContent = `${stats.week.completions} completions · ${stats.week.xp} XP`;
        document.getElementById('sqWeekDates').textContent = `${stats.week.start_date} – ${stats.week.end_date} · Mon–Sun, Hong Kong`;
        document.getElementById('sqWeekList').innerHTML = stats.week.quests.length
            ? stats.week.quests.map(quest => `<div class="sq-week-row"><span>${sqEscape(quest.title)}</span><strong>${quest.completions}</strong></div>`).join('')
            : '<div class="empty">No completions this week yet.</div>';
        document.getElementById('sqManageCount').textContent = `(${quests.length})`;
        document.getElementById('sqManageList').innerHTML = quests.length
            ? quests.map(quest => sqCard(quest, false, true)).join('')
            : '<div class="empty">Your Side Quests will appear here.</div>';
        document.getElementById('sqCompactError').textContent = '';
        document.getElementById('sqPageError').textContent = '';
        sqSetBusy(sqState.busy);
    } catch (error) {
        for (const id of ['sqCompactError', 'sqPageError']) {
            document.getElementById(id).textContent = 'Could not refresh Side Quests. ' + error.message;
        }
    } finally {
        sqState.loading = false;
        if (sqState.queued) { sqState.queued = false; loadSideQuests(); }
    }
}

async function sqMutate(path, options) {
    if (sqState.busy) return;
    sqSetBusy(true);
    try {
        await sqRequest(path, options);
    } catch (error) {
        // Reload canonical state after an error so a failed checkbox never stays ticked.
        await loadSideQuests();
        for (const id of ['sqCompactError', 'sqPageError']) document.getElementById(id).textContent = error.message;
        return;
    } finally {
        sqSetBusy(false);
    }
    await loadSideQuests();
}

function sqToggle(id, checkbox) {
    const complete = checkbox.checked;
    // Keep the last confirmed state until the server accepts the change.
    checkbox.checked = !complete;
    return sqMutate(`/${id}/complete`, { method: complete ? 'POST' : 'DELETE' });
}

function sqSetActive(id, active) {
    return sqMutate(`/${id}`, active
        ? { method: 'PUT', body: JSON.stringify({ active: true }) }
        : { method: 'DELETE' });
}

function sqScheduleChanged() {
    const weekly = document.getElementById('sqSchedule').value === 'Weekly';
    document.getElementById('sqWeekdays').classList.toggle('hidden', !weekly);
    document.getElementById('sqWeekdays').disabled = !weekly;
}

function sqOpenModal(id = null) {
    if (sqState.busy) return;
    const quest = id === null ? null : sqState.quests.find(item => item.id === id);
    if (id !== null && !quest) { loadSideQuests(); return; }
    sqState.editingId = id;
    sqState.lastFocus = document.activeElement;
    document.getElementById('sqForm').reset();
    document.getElementById('sqModalTitle').textContent = quest ? 'Edit Side Quest' : 'New Side Quest';
    document.getElementById('sqSaveButton').textContent = quest ? 'Save Changes' : 'Create Side Quest';
    document.getElementById('sqTitle').value = quest?.title || '';
    document.getElementById('sqDescription').value = quest?.description || '';
    document.getElementById('sqXP').value = quest?.xp ?? 10;
    document.getElementById('sqSchedule').value = quest?.schedule_type || 'Daily';
    document.getElementById('sqActive').checked = quest?.active ?? true;
    document.querySelectorAll('[name="sqWeekday"]').forEach(input => {
        input.checked = (quest?.weekdays || []).includes(input.value);
    });
    document.getElementById('sqFormError').textContent = '';
    sqScheduleChanged();
    document.getElementById('sqModal').classList.add('show');
    document.getElementById('sqTitle').focus();
}

function sqCloseModal() {
    if (sqState.busy) return;
    document.getElementById('sqModal').classList.remove('show');
    sqState.lastFocus?.focus();
}

async function sqSave(event) {
    event.preventDefault();
    if (sqState.busy) return;
    const schedule = document.getElementById('sqSchedule').value;
    const weekdays = Array.from(document.querySelectorAll('[name="sqWeekday"]:checked'), input => input.value);
    const title = document.getElementById('sqTitle').value.trim();
    const xp = Number(document.getElementById('sqXP').value);
    if (!title || !Number.isSafeInteger(xp) || xp < 1 || xp > 1000 || (schedule === 'Weekly' && !weekdays.length)) {
        document.getElementById('sqFormError').textContent = 'Enter a title, XP from 1 to 1000, and at least one day for a weekly quest.';
        return;
    }
    const payload = { title, description: document.getElementById('sqDescription').value.trim() || null,
        xp, schedule_type: schedule, weekdays: schedule === 'Weekly' ? weekdays : null,
        active: document.getElementById('sqActive').checked };
    sqSetBusy(true);
    let saved = false;
    try {
        await sqRequest(sqState.editingId === null ? '' : `/${sqState.editingId}`, {
            method: sqState.editingId === null ? 'POST' : 'PUT', body: JSON.stringify(payload)
        });
        saved = true;
    } catch (error) {
        document.getElementById('sqFormError').textContent = error.message;
    } finally { sqSetBusy(false); }
    if (saved) { sqCloseModal(); await loadSideQuests(); }
}

document.addEventListener('keydown', event => {
    const modal = document.getElementById('sqModal');
    if (!modal.classList.contains('show')) return;
    if (event.key === 'Escape') sqCloseModal();
    if (event.key !== 'Tab') return;
    const controls = Array.from(modal.querySelectorAll('button, input, textarea, select'))
        .filter(element => !element.disabled && element.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

function sqRefreshIfNewDay() {
    if (document.visibilityState !== 'visible') return;
    const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Hong_Kong',
        year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
    const get = type => parts.find(part => part.type === type).value;
    const date = `${get('year')}-${get('month')}-${get('day')}`;
    if (date !== sqState.date) loadSideQuests();
}
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') loadSideQuests();
});
setInterval(sqRefreshIfNewDay, 60000);
