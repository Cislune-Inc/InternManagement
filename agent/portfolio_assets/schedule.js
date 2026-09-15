/* Pure planning math. No clock, payroll, network or approval side effects. */
(function(root){
'use strict';
function validate(plan){
 if(!plan || plan.schema_version!==1 || !Array.isArray(plan.tasks) || !Array.isArray(plan.projects)) throw Error('Expected portfolio schema version 1.');
 if(plan.tasks.length>200) throw Error('Keep the planning view to 200 work packets.');
 const ids=new Set(), projects=new Set(plan.projects.map(p=>p.id));
 if(projects.size!==plan.projects.length || plan.projects.some(p=>typeof p.id!=='string'||!p.id)) throw Error('Project IDs must be unique nonempty strings.');
 for(const t of plan.tasks){
  if(typeof t.id!=='string' || !t.id || ids.has(t.id)) throw Error('Task IDs must be unique.'); ids.add(t.id);
  if(!projects.has(t.project)) throw Error('Unknown project for '+t.id);
  if(!Array.isArray(t.deps)||!Array.isArray(t.resources)||t.deps.some(x=>typeof x!=='string')||t.resources.some(x=>typeof x!=='string'||!x)) throw Error('Dependencies and resources must be lists.');
  for(const k of ['minimum','likely','downside']) if(t[k]!==null && (!Number.isFinite(t[k])||t[k]<0||t[k]>1000)) throw Error('Invalid duration for '+t.id);
  if(t.minimum!==null && t.likely!==null && t.minimum>t.likely || t.likely!==null && t.downside!==null && t.likely>t.downside || t.minimum!==null && t.downside!==null && t.minimum>t.downside) throw Error('Use minimum ≤ likely ≤ downside for '+t.id);
  if(!Number.isFinite(t.release)||t.release<0) throw Error('Invalid earliest start for '+t.id);
 }
 for(const t of plan.tasks) if(t.deps.some(id=>!ids.has(id)||id===t.id)) throw Error('Missing or self dependency for '+t.id);
 const done=new Set(), order=[];
 while(order.length<plan.tasks.length){
  const ready=plan.tasks.filter(t=>!done.has(t.id)&&t.deps.every(id=>done.has(id))).sort((a,b)=>(a.priority||100)-(b.priority||100)||plan.tasks.indexOf(a)-plan.tasks.indexOf(b));
  if(!ready.length) throw Error('Dependency cycle: revise the predecessor links.');
  done.add(ready[0].id); order.push(ready[0]);
 }
 return order;
}
function schedule(plan,mode='likely',levelResources=true){
 const order=validate(plan), rows=Object.create(null), reservations=Object.create(null);
 for(const t of order){
  const deps=t.deps.map(id=>rows[id]);
  const duration=t.status==='done'?0:t[mode];
  const conditional=t.status!=='done' && (Boolean(t.blocker)||!t.availability_confirmed||deps.some(d=>d.conditional));
  const depth=deps.length?Math.max(...deps.map(d=>d.depth))+1:0;
  if(duration===null || (t.status!=='done'&&deps.some(d=>d.finish===null))){rows[t.id]={start:null,finish:null,duration,depth,conditional:true,resourceDelay:0,resourcePredecessors:[]};continue;}
  const logicStart=t.status==='done'?0:Math.max(t.release,...deps.map(d=>d.finish||0));
  let start=logicStart;
  if(levelResources&&duration>0){
   let changed=true;
   while(changed){changed=false;
    for(const r of t.resources) for(const slot of reservations[r]||[]){
     if(start<slot.finish-1e-9 && start+duration>slot.start+1e-9){start=slot.finish;changed=true;}
    }
   }
  }
  const resourcePredecessors=levelResources?t.resources.flatMap(r=>(reservations[r]||[]).filter(s=>Math.abs(s.finish-start)<1e-9).map(s=>s.id)):[];
  rows[t.id]={start,finish:start+duration,duration,depth,conditional,resourceDelay:start-logicStart,resourcePredecessors:[...new Set(resourcePredecessors)]};
  if(levelResources&&duration>0)for(const r of t.resources){(reservations[r]||=[]).push({id:t.id,start,finish:start+duration});reservations[r].sort((a,b)=>a.start-b.start);}
 }
 const finish=Math.max(0,...Object.values(rows).map(r=>r.finish||0));
 // Driving chain includes resource waits in this particular greedy scenario.
 const driving=new Set();
 function trace(id){if(driving.has(id))return;driving.add(id);const t=plan.tasks.find(t=>t.id===id),r=rows[id];for(const p of [...t.deps,...r.resourcePredecessors])if(rows[p].finish!==null&&Math.abs(rows[p].finish-r.start)<1e-8)trace(p);}
 for(const t of order)if(rows[t.id].finish!==null&&Math.abs(rows[t.id].finish-finish)<1e-8)trace(t.id);
 const projectFinishes=Object.create(null);
 for(const p of plan.projects){const rr=plan.tasks.filter(t=>t.project===p.id).map(t=>rows[t.id]);projectFinishes[p.id]=rr.length&&rr.every(r=>r.finish!==null)?Math.max(...rr.map(r=>r.finish)):null;}
 return {rows,order:order.map(t=>t.id),finish,driving:[...driving],projectFinishes,incomplete:Object.values(rows).some(r=>r.finish===null)};
}
function workDate(start,offset){
 if(!/^\d{4}-\d{2}-\d{2}$/.test(start)||!Number.isFinite(offset)) return 'Unscheduled';
 const d=new Date(start+'T12:00:00Z');if(!Number.isFinite(+d))return 'Unscheduled';
 while([0,6].includes(d.getUTCDay()))d.setUTCDate(d.getUTCDate()+1);
 let days=Math.max(0,Math.ceil(offset)-1);while(days){d.setUTCDate(d.getUTCDate()+1);if(![0,6].includes(d.getUTCDay()))days--;}
 return d.toISOString().slice(0,10);
}
const api={validate,schedule,workDate};if(typeof module!=='undefined')module.exports=api;root.PortfolioSchedule=api;
})(typeof globalThis!=='undefined'?globalThis:this);
