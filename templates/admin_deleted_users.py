{% extends "base_admin.html" %}
{% block title %}Deleted users — Admin{% endblock %}
{% block header %}Deleted Users{% endblock %}

{% block content %}
  {% if not users %}
    <div style="padding:28px;border-radius:12px;background:linear-gradient(180deg,var(--panel),#042033);text-align:center">
      <h3 style="margin:0 0 6px;color:white">No deleted users</h3>
      <p style="margin:0;color:var(--muted)">There are currently no deleted user accounts.</p>
    </div>
  {% else %}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px">
      {% for u in users %}
        {% set p = u.profile %}
        <article id="del-card-{{ u.id }}" style="background:var(--card);padding:14px;border-radius:12px;display:flex;gap:12px;align-items:flex-start;box-shadow:0 6px 20px rgba(2,6,23,0.6)">
          <div style="width:86px;height:86px;border-radius:10px;background:var(--glass);overflow:hidden;flex:0 0 86px;display:flex;align-items:center;justify-content:center">
            {% if p and p.profile_photo %}
              <img src="{{ url_for('admin_serve_uploads', filename=p.profile_photo) }}" alt="{{ u.first_name }}" style="width:100%;height:100%;object-fit:cover">
            {% else %}
              <img src="{{ url_for('static', filename='default-avatar.png') }}" alt="avatar" style="width:100%;height:100%;object-fit:cover">
            {% endif %}
          </div>

          <div style="flex:1">
            <div style="display:flex;justify-content:space-between;align-items:start;gap:12px">
              <div>
                <div style="font-weight:700;color:white;font-size:15px">{{ u.first_name }} {{ u.last_name }}</div>
                <div style="margin-top:6px;color:var(--muted);font-size:13px">{{ u.email }}</div>
              </div>

              <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-end">
                <a class="btn view" href="{{ url_for('admin_view_user', user_id=u.id) }}" style="text-decoration:none;padding:8px 10px;border-radius:8px;background:rgba(255,255,255,0.03);color:var(--muted)">View</a>

                <button class="btn" type="button"
                        data-url="{{ url_for('admin_restore_user', user_id=u.id) }}"
                        style="background:var(--accent);border-radius:8px;padding:8px 10px;color:white;border:0;cursor:pointer"
                        onclick="restoreFromList(this.dataset.url, {{ u.id }})">
                  Restore
                </button>

                <button class="btn" type="button"
                        data-url="{{ url_for('admin_permanent_delete_user', user_id=u.id) }}"
                        style="background:var(--danger);border-radius:8px;padding:8px 10px;color:white;border:0;cursor:pointer"
                        onclick="permanentDeleteFromList(this.dataset.url, {{ u.id }})">
                  Permanent delete
                </button>
              </div>
            </div>

            <div style="margin-top:10px;color:var(--muted);font-size:13px">
              Role: {{ u.role }} &nbsp;•&nbsp; Verified: {{ 'Yes' if u.verified else 'No' }}
            </div>
          </div>
        </article>
      {% endfor %}
    </div>
  {% endif %}
{% endblock %}

{% block scripts %}
<script>
  function getCsrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  }
  async function jsonOrText(res) {
    const txt = await res.text();
    try { return JSON.parse(txt); } catch(e) { return txt; }
  }

  async function restoreFromList(url, uid) {
    if (!confirm('Restore this user?')) return;
    try {
      const res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken(),
          'X-CSRF-Token': getCsrfToken()
        },
        body: JSON.stringify({ confirm: true })
      });
      if (res.redirected) { alert('Not authorized — you were redirected to login.'); return; }
      if (!res.ok) { const parsed = await jsonOrText(res); alert('Restore failed: ' + (parsed && parsed.message ? parsed.message : res.statusText)); return; }
      const data = await res.json();
      if (data && data.status === 'ok') {
        const el = document.getElementById('del-card-' + uid);
        if (el) el.remove();
        alert('User restored.');
      } else {
        alert('Restore failed: ' + (data && data.message ? data.message : 'unknown'));
      }
    } catch (err) {
      console.error(err);
      alert('Request failed: ' + err);
    }
  }

  async function permanentDeleteFromList(url, uid) {
    if (!confirm('Permanently delete this user? This cannot be undone.')) return;
    try {
      const res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken(),
          'X-CSRF-Token': getCsrfToken()
        },
        body: JSON.stringify({ confirm: true })
      });
      if (res.redirected) { alert('Not authorized — you were redirected to login.'); return; }
      if (!res.ok) { const parsed = await jsonOrText(res); alert('Permanent delete failed: ' + (parsed && parsed.message ? parsed.message : res.statusText)); return; }
      const data = await res.json();
      if (data && data.status === 'ok') {
        const el = document.getElementById('del-card-' + uid);
        if (el) el.remove();
        alert('User permanently deleted.');
      } else {
        alert('Permanent delete failed: ' + (data && data.message ? data.message : 'unknown'));
      }
    } catch (err) {
      console.error(err);
      alert('Request failed: ' + err);
    }
  }
</script>
{% endblock %}
