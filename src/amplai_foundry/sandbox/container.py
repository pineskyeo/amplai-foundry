"""Pinned-image container process boundary for untrusted generated code.

Runtime configuration must explicitly qualify an egress-controlled network before
network access is enabled. No privileged mode, host socket, home mount, or host PID.
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from amplai_foundry.runtime.errors import Hold,RuntimeFault

@dataclass(frozen=True)
class ContainerProfile:
    image: str
    uid: int=65534
    gid: int=65534
    memory: str='1g'
    cpus: float=1.0
    pids: int=128
    network: str='none'
    network_qualification_ref: dict | None=None

class ContainerSandbox:
    def __init__(self,profile: ContainerProfile, *,engine: str='docker'):
        if not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}',profile.image):
            raise Hold('IMAGE_NOT_PINNED','An immutable registry image digest is required')
        if profile.uid<=0 or profile.gid<=0 or not 0<profile.cpus<=64 or not 1<=profile.pids<=4096:
            raise RuntimeFault('CONTAINER_PRIVILEGE','Non-root bounded container profile required')
        if profile.network!='none' and not profile.network_qualification_ref:
            raise Hold('EGRESS_UNQUALIFIED','Network access requires a qualified enforced egress profile')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',profile.network): raise RuntimeFault('NETWORK_NAME','Invalid container network')
        if engine not in {'docker','podman'}: raise RuntimeFault('CONTAINER_ENGINE','Only configured docker or podman executables are accepted')
        if not re.fullmatch(r'[1-9][0-9]*[kKmMgG]?',profile.memory): raise RuntimeFault('CONTAINER_MEMORY','Positive bounded memory is required')
        self.profile,self.engine=profile,engine
    def command(self,argv: list[str],workspace: Path,run_name: str, *,env_names: list[str] | None=None,interactive: bool=False,native_home: Path | None=None,readonly_mounts: dict[str,Path] | None=None) -> list[str]:
        if not argv or any('\x00' in a for a in argv): raise RuntimeFault('COMMAND_ARGV','A nonempty argv vector is required')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',run_name): raise RuntimeFault('CONTAINER_NAME','Invalid run name')
        original=Path(workspace).absolute()
        if original.resolve()!=original or not original.is_dir(): raise RuntimeFault('WORKSPACE_PATH','Workspace must be an existing non-symlink directory')
        workspace=original
        if ',' in str(workspace): raise RuntimeFault('MOUNT_PATH','Unsupported comma in mount path')
        p=self.profile
        args=[self.engine,'run','--name',run_name,'--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
              '--user',f'{p.uid}:{p.gid}','--network',p.network,'--memory',p.memory,'--cpus',str(p.cpus),'--pids-limit',str(p.pids),
              '--tmpfs','/tmp:rw,nosuid,nodev,size=268435456','--tmpfs','/home/agent:rw,nosuid,nodev,size=67108864',
              '--env','HOME=/home/agent','--mount',f'type=bind,src={workspace},dst=/workspace','--workdir','/workspace']
        if interactive: args.append('-i')
        if native_home is not None:
            home=Path(native_home).absolute()
            if home.resolve()!=home or not home.is_dir() or ',' in str(home): raise RuntimeFault('SESSION_HOME','Session home must be an explicit existing non-symlink directory')
            # Replace only the ephemeral /home/agent mount, never mount a user's HOME.
            pos=args.index('/home/agent:rw,nosuid,nodev,size=67108864');del args[pos-1:pos+1]
            args+=['--mount',f'type=bind,src={home},dst=/home/agent']
        for target,source in (readonly_mounts or {}).items():
            source=Path(source).absolute()
            if not target.startswith('/amplai-input/') or '..' in Path(target).parts or ',' in target or source.resolve()!=source or ',' in str(source):
                raise RuntimeFault('READONLY_MOUNT','Trusted input mounts must remain under /amplai-input')
            args+=['--mount',f'type=bind,src={source},dst={target},readonly']
        for name in env_names or []:
            if name in {'HOME','PATH','LD_PRELOAD','LD_LIBRARY_PATH','PYTHONPATH','NODE_OPTIONS','DOCKER_HOST','DOCKER_CONTEXT'}: raise Hold('ENV_AUTHORITY','Environment cannot override the sandbox runtime')
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',name): raise RuntimeFault('ENV_NAME','Invalid injected environment key')
            args+=['--env',name]
        return args+[p.image,*argv]
    def probe(self) -> dict:
        try:
            result=subprocess.run([self.engine,'image','inspect',self.profile.image],capture_output=True,text=True,timeout=15,check=False)
        except (OSError,subprocess.TimeoutExpired) as exc: raise Hold('CONTAINER_UNAVAILABLE','Container runtime could not be probed') from exc
        return {'image':self.profile.image,'present':result.returncode==0,'qualified':False,
                'reason':'Image availability alone is not an escape/egress qualification'}
    def stop(self,name: str):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name): raise RuntimeFault('CONTAINER_NAME','Invalid run name')
        result=subprocess.run([self.engine,'stop','--time','5',name],capture_output=True,text=True,timeout=15,check=False)
        if result.returncode: raise Hold('STOP_UNCONFIRMED','Container stop has not been confirmed')
        if not self.stopped(name): raise Hold('STOP_UNCONFIRMED','Container still reports a running process')
    def stopped(self,name: str) -> bool:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name): raise RuntimeFault('CONTAINER_NAME','Invalid run name')
        import json
        try:
            result=subprocess.run([self.engine,'container','inspect',name],capture_output=True,text=True,timeout=15,check=False)
            if result.returncode: return False  # absence is not proof for an unknown spawn
            values=json.loads(result.stdout)
            return bool(values) and all(v.get('State',{}).get('Running') is False for v in values)
        except (OSError,ValueError,subprocess.TimeoutExpired): return False
    def destroy(self,name: str):
        if not self.stopped(name): raise Hold('DESTROY_RUNNING','Container must be positively stopped before removal')
        result=subprocess.run([self.engine,'container','rm',name],capture_output=True,text=True,timeout=15,check=False)
        if result.returncode: raise Hold('CONTAINER_REMOVE','Container removal failed')
