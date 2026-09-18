"""Actual browser rendering, geometry, functionality and visual regression checks.

Image similarity is not a proxy for aesthetic quality. Subjective acceptance stays
an independent human/verifier obligation in the Goal Contract.
"""
from __future__ import annotations
import hashlib,json,tempfile
from pathlib import Path
from dataclasses import dataclass
from .service import VerificationObservation
from ...runtime.contracts.identity import digest_bytes,digest
from ...runtime.errors import Hold,RuntimeFault

@dataclass(frozen=True)
class BrowserPolicy:
    browser: str='chromium'
    executable_path: str | None=None
    timeout_ms: int=15000
    max_html_bytes: int=8*1024*1024
    execute_scripts: bool=False
    # Test-only escape hatch bound to exact reviewed bytes, not a global switch.
    isolated_test_fixture_digest: str | None=None

class BrowserRenderer:
    def __init__(self,policy: BrowserPolicy=BrowserPolicy()):self.policy=policy
    def render(self,html: bytes,destination, *,width=1280,height=900,steps=None,required_selectors=None):
        if len(html)>self.policy.max_html_bytes or not 320<=width<=3840 or not 240<=height<=2160:raise Hold('RENDER_BOUNDS','HTML/viewport exceeds qualified renderer bounds')
        html.decode('utf-8');target=Path(destination);target.mkdir(parents=True,exist_ok=True)
        from playwright.sync_api import sync_playwright
        reviewed_test=self.policy.isolated_test_fixture_digest==digest_bytes(html)
        if self.policy.isolated_test_fixture_digest and not reviewed_test:raise Hold('TEST_FIXTURE_BINDING','Sandbox exception only applies to the exact reviewed test fixture')
        errors=[];blocked=[]
        with sync_playwright() as p:
            browser_type=getattr(p,self.policy.browser,None)
            if browser_type is None:raise Hold('BROWSER_PROFILE','Unknown browser implementation')
            try:browser=browser_type.launch(headless=True,chromium_sandbox=not reviewed_test,executable_path=self.policy.executable_path)
            except Exception as exc:raise Hold('BROWSER_UNAVAILABLE','Qualified browser/sandbox is not installed or cannot launch') from exc
            try:
                context=browser.new_context(viewport={'width':width,'height':height},java_script_enabled=self.policy.execute_scripts,service_workers='block')
                context.route('**/*',lambda route:(blocked.append(route.request.url),route.abort()))
                page=context.new_page();page.set_default_timeout(self.policy.timeout_ms)
                page.on('pageerror',lambda e:errors.append(str(e)[:512]))
                page.set_content(html.decode('utf-8'),wait_until='load',timeout=self.policy.timeout_ms)
                page.evaluate('document.fonts.ready')
                for step in steps or []:
                    if set(step)-{'action','selector','value'}:raise RuntimeFault('RENDER_STEP','Unknown functional test action fields')
                    action=step['action'];locator=page.locator(step['selector'])
                    if action=='click':locator.click()
                    elif action=='fill':locator.fill(step.get('value',''))
                    elif action=='check':locator.check()
                    elif action=='visible':
                        if not locator.is_visible():errors.append('Required interaction result not visible: '+step['selector'])
                    elif action=='text':
                        if locator.inner_text()!=step.get('value'):errors.append('Interaction text differs: '+step['selector'])
                    else:raise RuntimeFault('RENDER_STEP','Functional verifier does not execute arbitrary submitted JavaScript')
                details=page.evaluate("""() => {
                  const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0};
                  const critical=[...document.querySelectorAll('[data-amplai-critical]')].filter(visible);
                  const clipped=critical.filter(e=>e.scrollWidth>e.clientWidth+2||e.scrollHeight>e.clientHeight+2).map(e=>e.id||e.tagName);
                  const overflow=critical.filter(e=>{const r=e.getBoundingClientRect();return r.left< -1||r.right>innerWidth+1}).map(e=>e.id||e.tagName);
                  const overlaps=[];
                  for(let i=0;i<critical.length;i++)for(let j=i+1;j<critical.length;j++){
                    const a=critical[i],b=critical[j];if(a.contains(b)||b.contains(a))continue;
                    const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
                    if(Math.min(x.right,y.right)-Math.max(x.left,y.left)>2&&Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)>2)overlaps.push([a.id,b.id]);
                  }
                  return {horizontal_overflow:document.documentElement.scrollWidth>innerWidth+1,
                    clipped_critical:clipped,outside_critical:overflow,critical_overlaps:overlaps,
                    broken_images:[...document.images].filter(i=>!i.complete||!i.naturalWidth).map(i=>i.getAttribute('src')),
                    font_status:document.fonts.status,font_failures:[...document.fonts].filter(f=>f.status==='error').map(f=>f.family),
                    title:document.title,language:document.documentElement.lang,visible_text:document.body.innerText.slice(0,20000),
                    invalid_buttons:[...document.querySelectorAll('button')].filter(e=>visible(e)&&!e.innerText.trim()&&!e.getAttribute('aria-label')).length,
                    unlabelled_inputs:[...document.querySelectorAll('input:not([type=hidden])')].filter(e=>visible(e)&&!e.labels?.length&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')).length};
                }""")
                missing=[selector for selector in required_selectors or [] if page.locator(selector).count()==0 or not page.locator(selector).first.is_visible()]
                screenshot=target/'render.png';page.screenshot(path=str(screenshot),full_page=True,animations='disabled')
                result={'source_digest':digest_bytes(html),'viewport':{'width':width,'height':height},'browser_version':browser.version,
                        'details':details,'page_errors':errors,'blocked_requests_count':len(blocked),'missing_selectors':missing,
                        'screenshot_digest':digest_bytes(screenshot.read_bytes()),'screenshot_path':str(screenshot),
                        'execution_boundary':'reviewed exact fixture in isolated build container' if reviewed_test else 'browser sandbox',
                        'production_qualification':False,'aesthetic_acceptance':'not_assessed'}
                bad=errors or missing or details['horizontal_overflow'] or details['clipped_critical'] or details['outside_critical'] or details['critical_overlaps'] or details['broken_images'] or details['font_failures'] or details['invalid_buttons'] or details['unlabelled_inputs']
                result['outcome']='fail' if bad else 'pass'
                (target/'render-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
                return result
            finally:browser.close()
    @staticmethod
    def compare_golden(actual,golden, *,max_changed_fraction):
        if not 0<=max_changed_fraction<=1:raise RuntimeFault('GOLDEN_THRESHOLD','Difference threshold must be predeclared')
        from PIL import Image,ImageChops
        with Image.open(actual) as a,Image.open(golden) as b:
            if a.size!=b.size:return {'outcome':'fail','reason':'viewport/image size differs','aesthetic_acceptance':False}
            diff=ImageChops.difference(a.convert('RGB'),b.convert('RGB'))
            pixels=list(diff.getdata());changed=sum(max(p)>8 for p in pixels)/max(1,len(pixels))
        return {'outcome':'pass' if changed<=max_changed_fraction else 'fail','changed_fraction':changed,'threshold':max_changed_fraction,'aesthetic_acceptance':False}

class VisualVerifier:
    def __init__(self,renderer, *,viewports=((1280,900),(390,844)),required_selectors=(),steps=()):
        self.renderer,self.viewports,self.required,self.steps=renderer,viewports,required_selectors,steps
    def __call__(self,raw):
        results=[]
        with tempfile.TemporaryDirectory(prefix='amplai-render-') as temp:
            for width,height in self.viewports:
                result=self.renderer.render(raw,Path(temp)/str(width),width=width,height=height,required_selectors=self.required,steps=self.steps)
                # Embed evidence bytes into the observation for the trusted collector;
                # temporary paths are not durable artifact references.
                import base64
                result['screenshot_base64']=base64.b64encode(Path(result.pop('screenshot_path')).read_bytes()).decode()
                results.append(result)
        return VerificationObservation('pass' if all(x['outcome']=='pass' for x in results) else 'fail','Actual browser rendering and explicit interaction/geometry checks',{'renders':results,'aesthetic_acceptance':'requires separate human criterion'})

class DocumentFreshness:
    @staticmethod
    def verify(manifest,current_inputs):
        required={'source_digests','renderer_version','layout_profile','output_digest','golden_ref'}
        if set(manifest)!=required:return VerificationObservation('inconclusive','Document has no complete generation manifest',{})
        stale=[name for name,value in manifest['source_digests'].items() if current_inputs.get(name)!=value]
        missing=sorted(set(current_inputs)-set(manifest['source_digests']))
        return VerificationObservation('fail' if stale or missing else 'pass','Document source provenance and generation inputs',{'stale_sources':stale,'untracked_inputs':missing})
