const assert=require('node:assert/strict');const S=require('../agent/portfolio_assets/schedule.js');
const task=(id,deps=[],resources=[],d=1)=>({id,project:'p',title:id,deps,resources,minimum:d,likely:d,downside:d,release:0,status:'proposed',availability_confirmed:true});
const plan=tasks=>({schema_version:1,projects:[{id:'p'}],tasks});
let p=plan([task('a',[],['one'],2),task('b',[],['two'],3),task('c',['a','b'],['one'],1)]),s=S.schedule(p);
assert.equal(s.finish,4);assert.equal(s.rows.c.start,3);assert.deepEqual(new Set(s.driving),new Set(['b','c']));
p=plan([task('a',[],['one'],2),task('b',[],['one'],3)]);assert.equal(S.schedule(p).finish,5);assert.equal(S.schedule(p,'likely',false).finish,3);assert.equal(S.schedule(p).rows.b.resourceDelay,2);
p=plan([task('a',[],['one'],2),task('b',[],['one'],1)]);p.tasks[0].release=5;assert.equal(S.schedule(p).rows.b.start,0); // fill idle gap
p=plan([task('a',[],[],null),task('b',['a'])]);assert.equal(S.schedule(p).rows.b.finish,null);assert.equal(S.schedule(p).projectFinishes.p,null);
p.tasks[0].status='done';assert.equal(S.schedule(p).rows.b.finish,1);
p=plan([task('a',['b']),task('b',['a'])]);assert.throws(()=>S.validate(p),/cycle/);
p=plan([task('a',['missing'])]);assert.throws(()=>S.validate(p),/Missing/);
p=plan([task('a'),task('a')]);assert.throws(()=>S.validate(p),/unique/);
p=plan([task('a')]);p.tasks[0].minimum=2;assert.throws(()=>S.validate(p),/minimum/);
p=plan([task('a'),task('b',['a'])]);p.tasks[0].blocker='gate';assert.equal(S.schedule(p).rows.b.conditional,true);
assert.equal(S.workDate('2026-09-11',2),'2026-09-14');assert.equal(S.workDate('2026-09-12',1),'2026-09-14');
console.log('Schedule checks passed: parallel joins, resource contention/idle gaps, unknown propagation, completed work, cycles, invalid links/estimates, conditional gates, weekends.');
p=plan([task('__proto__',[],['__proto__'],1),task('z',['__proto__'],['__proto__'],1)]);assert.equal(S.schedule(p).finish,2);
p=plan([task('a')]);p.tasks[0].likely=null;p.tasks[0].minimum=5;assert.throws(()=>S.validate(p),/minimum/);
