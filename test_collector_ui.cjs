const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/trading_ui.html','utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new vm.Script(script);
// No automatic browser requests remain: every POST is behind a click handler.
for(const name of ['heartbeat','activeHeartbeat','stockHeartbeat'])assert(!new RegExp('setInterval\\(\\(?\\)?=?>?\\s*'+name).test(script)&&!script.includes('then('+name+')'),name+' still scheduled');
assert(!/setInterval\([^)]*post\(/.test(script),'a timer posts to the server');
assert(!html.includes('Keep this dashboard open'),'obsolete open-browser wording');
assert(!html.includes('while this dashboard is open')&&!html.includes('while this dashboard stays open'),'obsolete open-browser wording');
assert(html.includes('computer must be switched on, awake and online'));
const start=script.indexOf('// Background collector status.'),end=script.indexOf('// Stock paper accounts:');
const block=script.slice(start,end).replace('collectorRefresh();setInterval(collectorRefresh,15000);','');
const nodes={},tables={};let payload=null,fail=false;
const ctx={Number,Date,Math,console,paused:false,collectorMode:'',el(id){return nodes[id]??={textContent:'',className:''}},healthStamp:v=>'T('+v+')',
 fill(id,headers,rows,empty){tables[id]={headers,rows,empty}},fetch:async()=>{if(fail)throw Error('down');return {ok:true,json:async()=>payload}}};
vm.createContext(ctx);vm.runInContext(block+';this.collectorText=collectorText;',ctx);
const family=(o)=>Object.assign({label:'Core',state:'up_to_date',state_label:'Current',detail:'Session recorded.',running:false,hung:false,last_attempt_at:'2026-09-28T20:20:00+00:00',last_source:'scheduler',last_outcome:'no_change',last_new_observation_at:'2026-09-28T20:20:00+00:00',checks:4,new_observations:1},o);
(async()=>{
 payload={enabled:true,running:true,pid:42,started_at:'x',heartbeat_age_seconds:5,families:[family({})]};
 await ctx.collectorRefresh();
 assert.equal(ctx.collectorMode,'Automatic background collection');assert.equal(nodes['mode'].textContent,'Automatic background collection');
 assert.match(nodes['collector-summary'].textContent,/process 42/);assert.equal(nodes['collector-summary'].className,'muted');
 const row=tables['collector-families'].rows[0];assert.equal(row[1],'Current');assert.match(row[3],/scheduler · no new bar/);assert.equal(row[5],'4 checks / 1 new');
 payload.families=[family({state:'overdue',state_label:'Overdue',label:'Stocks 5m'}),family({state:'hung',state_label:'Unhealthy: request not returning',label:'Active',running:true,hung:true})];
 await ctx.collectorRefresh();
 assert.match(ctx.collectorMode,/attention needed/);assert.match(nodes['collector-summary'].textContent,/Stocks 5m \(Overdue\).*Active/);assert.equal(nodes['collector-summary'].className,'negative');
 assert.match(tables['collector-families'].rows[1][3],/in progress/);
 payload={enabled:true,running:false,heartbeat_age_seconds:4000,note:'server stopped',families:[]};
 await ctx.collectorRefresh();assert.equal(ctx.collectorMode,'Background collector not running');assert.match(nodes['collector-summary'].textContent,/67 min ago.*server stopped/);
 payload={enabled:false,message:'disabled here'};await ctx.collectorRefresh();assert.equal(ctx.collectorMode,'Background collector off');assert.equal(tables['collector-families'].rows.length,0);
 fail=true;await ctx.collectorRefresh();assert.match(nodes['collector-summary'].textContent,/Server unreachable/);assert.equal(nodes['collector-summary'].className,'negative');
 console.log('PASS collector panel states, no automatic browser requests, independent-operation wording');
})().catch(e=>{console.error(e);process.exit(1)});
