{% extends "base.html" %}
{% block content %}
<div class="card">
  <h2>Тренировки</h2>
  <p><a class="btn sm" href="/admin/settings">⚙️ Настройки клуба</a></p>
  {% if trainings %}
  <table>
    <tr><th>#</th><th>Название</th><th>Дата</th><th>Мест</th><th>Цена</th><th></th></tr>
    {% for t in trainings %}
    <tr>
      <td>{{ t.id }}</td>
      <td>{{ t.title }}{% if t.state == 'draft' %} <span class="tag no">черновик</span>{% endif %}</td>
      <td>{{ t.when }}</td>
      <td>{{ t.active }}/{{ t.max_participants }}</td>
      <td>{% if t.price_minor %}{{ '%.2f'|format(t.price_minor/100) }} {{ t.currency }}{% else %}—{% endif %}</td>
      <td>
        <a class="btn sm" href="/admin/trainings/{{ t.id }}">Открыть</a>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">Тренировок пока нет.</p>
  {% endif %}
</div>
{% endblock %}
