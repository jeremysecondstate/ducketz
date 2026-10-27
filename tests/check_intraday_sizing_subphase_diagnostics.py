"""Offline stdlib checks for bounded inner sizing clocks and owned patch.
No production modules are imported; --installed-root verifies a sealed source.
"""
from __future__ import annotations
import argparse, ast, copy, hashlib, json, math, re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

TARGET='ml/intraday/account_execution.py'
PREFIX='intraday-sizing-subphase-diagnostics'
CHECKS=[]
def check(name, condition):
    if not condition: raise AssertionError(name)
    CHECKS.append(name)
def sha(raw): return hashlib.sha256(raw).hexdigest()

def helpers(text):
    text=text.replace("\r\n","\n")
    nodes=[n for n in ast.parse(text).body if getattr(n,'name',None) in {'_execution_loaded_generation','_SizingTimings'} or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_LOADED_EXECUTION_GENERATION' for t in n.targets)]
    check('three isolated helper nodes',len(nodes)==3)
    return '\n\n'.join(ast.get_source_segment(text,n) for n in nodes)+'\n'

class Clock:
    def __init__(self, wall=None,cpu=None,ids=None):
        self.wall=iter(wall if wall is not None else [0.,2.])
        self.cpu=iter(cpu if cpu is not None else [0.,.25])
        self.ids=iter(ids if ids is not None else [12,12])
    @staticmethod
    def item(values):
        value=next(values)
        if isinstance(value,BaseException): raise value
        return value
    def monotonic(self): return self.item(self.wall)
    def thread_time(self): return self.item(self.cpu)
    def get_ident(self): return self.item(self.ids)

def namespace(source, clock=None,module_file=None):
    clock=clock or Clock()
    ns=dict(Path=Path,contextmanager=contextmanager,math=math,time=clock,
            threading=clock,__file__=module_file or str(Path.cwd()/TARGET))
    exec(compile(source,'<extracted-sizing-diagnostics>','exec'),ns)
    return ns

def phase_tests(source):
    ns=namespace(source); T=ns['_SizingTimings']; t=T()
    with t.phase('frozen_inputs'): pass
    row=t.snapshot()['phases']['frozen_inputs']
    check('wall sample',row['total_seconds']==2 and row['elapsed_count']==1)
    check('thread cpu sample',row['thread_cpu_total_seconds']==.25 and row['thread_cpu_count']==1)
    check('one completed call',row['count']==1)
    check('clocks inclusive metadata',t.snapshot()['totals_are_inclusive'] is True)
    check('unsealed generation unknown',t.snapshot()['loaded_generation'] is None and t.snapshot()['source_kind']=='unsealed')
    snap=t.snapshot(); snap['phases']['frozen_inputs']['count']=999
    check('snapshot deeply owns phase rows',t.snapshot()['phases']['frozen_inputs']['count']==1)
    snap['phases']['extra']={}; check('snapshot owns phase mapping','extra' not in t.phases)
    t.frozen({'candles':[{},{}]}); check('candle row count',t.snapshot()['candle_rows']==2)
    class BadDict(dict):
        def get(self,*args): raise AssertionError('subclass callbacks forbidden')
    t.frozen(BadDict()); check('irregular frozen values ignored',t.candle_rows==2)
    ns=namespace(source,Clock([0.,1.,1.,3.],[0.,.2,.2,.5],[9]*4)); t=ns['_SizingTimings']()
    for unused in range(2):
        with t.phase('size_calculation'): pass
    row=t.snapshot()['phases']['size_calculation']
    check('repeat bounded same bucket',len(t.phases)==1 and row['count']==2)
    check('repeat totals',row['total_seconds']==3 and row['thread_cpu_total_seconds']==.5)
    check('repeat maxima',row['maximum_seconds']==2 and row['thread_cpu_maximum_seconds']==.3)
    failure=LookupError('original')
    ns=namespace(source); t=ns['_SizingTimings']()
    try:
        with t.phase('sizing_persistence'): raise failure
    except LookupError as got: check('exception identity preserved',got is failure)
    else: raise AssertionError('exception swallowed')
    check('failed operation phase retained',t.snapshot()['phases']['sizing_persistence']['count']==1)
    for bad in (None,float('nan'),float('inf'),'1',True,ValueError('clock')):
        ns=namespace(source,Clock([bad,2.],[bad,.25],[3,3])); t=ns['_SizingTimings']()
        with t.phase('intention_read'): pass
        snap=t.snapshot(); row=snap['phases']['intention_read']
        check('bad clocks flagged '+repr(bad),snap['elapsed_unavailable'] and snap['thread_cpu_unavailable'])
        check('bad clocks omit fabricated elapsed '+repr(bad),'total_seconds' not in row and 'thread_cpu_total_seconds' not in row)
    ns=namespace(source,Clock([5.,4.],[2.,1.],[9,9])); t=ns['_SizingTimings']()
    with t.phase('intention_read'): pass
    check('backward clocks flagged',t.elapsed_unavailable and t.thread_cpu_unavailable)
    ns=namespace(source,Clock([0.,2.],[0.,.25],[9,10])); t=ns['_SizingTimings']()
    with t.phase('intention_read'): pass
    row=t.snapshot()['phases']['intention_read']
    check('cross-thread wall retained',row['total_seconds']==2)
    check('cross-thread cpu never attributed','thread_cpu_total_seconds' not in row and t.thread_cpu_unavailable)
    ns=namespace(source,Clock([0.,2.],[0.,.25],[ValueError('thread'),9])); t=ns['_SizingTimings']()
    with t.phase('intention_read'): pass
    check('thread identity failure safe',t.thread_cpu_unavailable and t.snapshot()['phases']['intention_read']['total_seconds']==2)
    ns=namespace(source,Clock([0.,1.]*5,[0.,.1]*5,[1]*10)); t=ns['_SizingTimings']()
    for name in sorted(t._PHASES):
        with t.phase(name): pass
    with t.phase('unbounded-user-name'): pass
    check('fixed four phase inventory',len(t.phases)==4 and 'unbounded-user-name' not in t.phases)
    check('unknown stage flags diagnostic limitation',t.elapsed_unavailable and t.thread_cpu_unavailable)
    ns=namespace(source,Clock([0.,1.,0.,1.],[0.,.1,0.,.1],[1]*4)); t=ns['_SizingTimings']()
    t.phases['frozen_inputs']={'total_seconds':1.79e308}
    with t.phase('frozen_inputs'): pass
    check('large finite sums remain finite',math.isfinite(t.phases['frozen_inputs']['total_seconds']))
    # A broken diagnostic clock cannot replace the affected operation's error.
    ns=namespace(source,Clock([ValueError('w1'),ValueError('w2')],[ValueError('c1'),ValueError('c2')],[1,1])); t=ns['_SizingTimings']()
    try:
        with t.phase('frozen_inputs'): raise failure
    except LookupError as got: check('clock failures preserve original error',got is failure)


def generation_tests(source):
    ns=namespace(source); f=ns['_execution_loaded_generation']; key='a1'*32
    good=Path.cwd()/'generations'/key/'source'/'ml'/'intraday'/'account_execution.py'
    check('exact sealed layout identifies generation',f(str(good))==key)
    for path in [Path('generations')/key/'source'/'ml'/'intraday'/'account_execution.py',
                 Path.cwd()/'generations'/key.upper()/'source'/'ml'/'intraday'/'account_execution.py',
                 Path.cwd()/'generations'/'short'/'source'/'ml'/'intraday'/'account_execution.py',
                 Path.cwd()/'generations'/key/'source'/'ml'/'intraday'/'other.py',
                 Path.cwd()/'generations'/key/'staging'/'ml'/'intraday'/'account_execution.py',
                 Path.cwd()/'active.json',None]:
        check('unproven path stays unsealed '+str(path),f(path) is None)
    ns=namespace(source,module_file=str(good)); t=ns['_SizingTimings']()
    ns['__file__']='later-unsealed-edit'
    check('generation captured from loaded module at import',t.snapshot()['loaded_generation']==key and t.snapshot()['source_kind']=='sealed')
    calls={ast.unparse(n.func) for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Call)}
    check('generation proof performs no file io',not any(any(token in s for token in ('read_text','read_bytes','resolve','stat','open','active')) for s in calls))


def trace_tests(meta):
    import itertools
    source=meta['helper_source']; key='f0'*32
    module_file=str(Path.cwd()/'generations'/key/'source'/'ml'/'intraday'/'account_execution.py')
    def trace_ns(source):
        seq=itertools.count()
        clock=SimpleNamespace(monotonic=lambda:float(next(seq)),thread_time=lambda:0.,get_ident=lambda:1)
        ns=namespace(meta['helper_source'],clock,module_file)
        exec(compile(source,'<extracted-execution-trace>','exec'),ns)
        return ns
    for keyname in ('trace_before','trace_after'):
        check(keyname+' source digest exact',sha(meta[keyname].encode())==meta[keyname+'_sha256'])
    old=trace_ns(meta['trace_before'])['_ExecutionTrace'](); ns=trace_ns(meta['trace_after']); new=ns['_ExecutionTrace']()
    snap=new.snapshot()
    check('recovery-only trace loaded identity',snap['loaded_generation']==key and snap['source_kind']=='sealed' and 'sizing_subphase_timings' not in snap)
    # Fresh equal clocks retain the prior outer timing and bounded retention.
    old=trace_ns(meta['trace_before'])['_ExecutionTrace'](); new=trace_ns(meta['trace_after'])['_ExecutionTrace']()
    for unused in range(41):
        old.advance('sizing'); new.advance('sizing')
    before,after=old.snapshot(),new.snapshot()
    after.pop('loaded_generation'); after.pop('source_kind')
    check('outer trace timings counts and tail unchanged',before==after)
    check('original32tail retained',len(after['execution_phases'])==32 and after['execution_phase_summary']['dropped_tail_count']==9)
    class BrokenSnapshot:
        def snapshot(self): raise ValueError('optional diagnostic failure')
    new.sizing_timings=BrokenSnapshot(); check('optional diagnostic snapshot failure flagged',new.snapshot()['sizing_diagnostics_unavailable'] is True)
    tns=trace_ns(meta['trace_after']); trace=tns['_ExecutionTrace'](); trace.sizing_timings=tns['_SizingTimings']()
    with trace.sizing_timings.phase('sizing_persistence'): pass
    snap=trace.snapshot(); snap['sizing_subphase_timings']['phases']['sizing_persistence']['count']=99
    check('full trace nested snapshot deeply owned',trace.snapshot()['sizing_subphase_timings']['phases']['sizing_persistence']['count']==1)


def apply_patch(text,patch):
    original=text.replace('\r\n','\n').splitlines(True); output=[]; cursor=0; lines=patch.splitlines(True); i=2
    while i<len(lines):
        m=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[i])
        if not m: raise AssertionError('invalid hunk header')
        start=int(m.group(1))-1; output.extend(original[cursor:start]); cursor=start; i+=1
        while i<len(lines) and not lines[i].startswith('@@ '):
            mark,line=lines[i][0],lines[i][1:]
            if mark in (' ','-'):
                if original[cursor]!=line: raise AssertionError('patch preimage line')
                cursor+=1
            if mark in (' ','+'): output.append(line)
            if mark not in (' ','+','-'): raise AssertionError('patch marker')
            i+=1
    return ''.join(output+original[cursor:])

def owned_ast(before,after):
    class Strip(ast.NodeTransformer):
        def visit_Module(self,node):
            node.body=[n for n in node.body if getattr(n,'name',None) not in {'_execution_loaded_generation','_SizingTimings'} and not(isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_LOADED_EXECUTION_GENERATION' for t in n.targets))]
            return self.generic_visit(node)
        def visit_ImportFrom(self,node):
            if node.module=='contextlib': node.names=[n for n in node.names if n.name!='nullcontext']
            return node
        def visit_Assign(self,node):
            text=ast.unparse(node.targets[0])
            if text in {'self.sizing_timings','sizing_timings','sizing_phase'}: return None
            return self.generic_visit(node)
        def visit_Call(self,node):
            if isinstance(node.func,ast.Name) and node.func.id=='dict':
                node.keywords=[kw for kw in node.keywords if kw.arg not in {'loaded_generation','source_kind'}]
            return self.generic_visit(node)
        def visit_If(self,node):
            if ast.unparse(node.test) in {'self._execution_trace is not None','sizing_timings is not None','self.sizing_timings is not None'}:
                # Only remove the specifically additive diagnostic blocks.
                text=ast.unparse(node)
                if any(s in text for s in ('self._execution_trace.sizing_timings = sizing_timings','sizing_timings.frozen(frozen[1])','result[\'sizing_subphase_timings\'] = self.sizing_timings.snapshot()')): return None
            return self.generic_visit(node)
        def visit_With(self,node):
            if len(node.items)==1 and isinstance(node.items[0].context_expr,ast.Call) and isinstance(node.items[0].context_expr.func,ast.Name) and node.items[0].context_expr.func.id=='sizing_phase':
                return [self.visit(n) for n in node.body]
            return self.generic_visit(node)
    check('all non-diagnostic module AST preserved',ast.dump(ast.parse(before),include_attributes=False)==ast.dump(Strip().visit(ast.parse(after)),include_attributes=False))


def main():
    p=argparse.ArgumentParser(); p.add_argument('--application-root',type=Path); p.add_argument('--installed-root',type=Path); p.add_argument('--before',type=Path); p.add_argument('--after',type=Path); args=p.parse_args()
    root=Path(__file__).resolve().parents[1]; folder=root/'docs'/'development'/'patches'
    meta=json.loads((folder/(PREFIX+'.json')).read_text(encoding='utf-8')); patch=(folder/(PREFIX+'.patch')).read_bytes()
    check('schema exact',meta['schema']==PREFIX+'-v1'); check('one owned target',meta['target']==TARGET)
    check('patch digest exact',sha(patch)==meta['patch_sha256']); check('helper digest exact',sha(meta['helper_source'].encode())==meta['helper_sha256'])
    lines=patch.decode().splitlines(); check('exact target patch headers',lines[:2]==['--- a/'+TARGET,'+++ b/'+TARGET]); check('exact hunk count',sum(s.startswith('@@ ') for s in lines)==meta['expected_hunks']==7)
    check('single patch target',sum(s.startswith('--- ') for s in lines)==sum(s.startswith('+++ ') for s in lines)==1)
    phase_tests(meta['helper_source']); generation_tests(meta['helper_source']); trace_tests(meta)
    if args.before and args.after:
        braw=args.before.read_bytes(); araw=args.after.read_bytes()
        check('reviewed exact preimage',sha(braw)==meta['before_sha256']); check('reviewed exact final',sha(araw)==meta['after_sha256'])
        before=braw.decode(); after=araw.decode(); check('full patch reconstructs final text',apply_patch(before,patch.decode())==after.replace('\r\n','\n')); owned_ast(before,after)
        check('full helper extraction exact',helpers(after)==meta['helper_source'])
    app=args.application_root
    if args.installed_root:
        manifest=json.loads((args.installed_root/'manifest.json').read_text(encoding='utf-8'))
        check('sealed manifest exists',isinstance(manifest,dict)); app=args.installed_root/'source'
    if app:
        raw=(app/TARGET).read_bytes(); check('actual target exact final hash',sha(raw)==meta['after_sha256']); check('actual isolated helpers exact',helpers(raw.decode())==meta['helper_source']); phase_tests(helpers(raw.decode()))
    print(json.dumps({'checks_passed':len(CHECKS),'checks':CHECKS,'network_calls':0,'production_imports':0,'limitations':'Extracted helper and patch checks; full composition regression and natural affected exercise recorded separately.'}))
if __name__=='__main__': main()