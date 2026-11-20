/** 业务窗页签模板片段 */
export const windowsViewTemplate = `
<div class="card wide" v-show="tab===0">
  <h2>窗口条带</h2>
  <p class="muted">关窗占窗锁；后到争用只记争用。关窗后才能物化。计数与仪表同读 dash_counter。</p>
  <p>开启 {{stripCounts.open_windows||0}} · 已关闭 {{stripCounts.closed_windows||0}} · 已重开 {{stripCounts.reopens||0}} <span class="muted">· 同源 {{stripCounts.source||'dash_counter'}}</span></p>
  <table>
    <thead><tr><th>窗口</th><th>世代</th><th>开启</th><th>关闭</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="w in windows" :key="w.id">
        <td>{{w.name}}</td>
        <td>{{w.generation}}</td>
        <td>{{w.open_at}}</td>
        <td>{{w.close_at}}</td>
        <td>{{w.status}}</td>
        <td>
          <button class="ghost" @click="closeWin(w.id)">关窗</button>
          <button class="ghost" @click="materialize(w.id)">物化</button>
          <button class="ghost" @click="loadRecon(w.id)">对账</button>
          <button class="ghost" @click="reopenStub(w.id)">重开（作废重算）</button>
        </td>
      </tr>
    </tbody>
  </table>
  <div class="err">{{actionErr}}</div>
</div>
`;
