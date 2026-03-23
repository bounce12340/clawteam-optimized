"""Cleanup utilities for ClawTeam - manages idle and completed agents."""

import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import psutil

from clawteam.team.manager import TeamManager
from clawteam.team.models import get_data_dir


def get_agent_processes(team_name: Optional[str] = None) -> List[dict]:
    """Get all ClawTeam agent processes.
    
    Returns list of processes with team_name, agent_name, pid, and start_time.
    """
    agents = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            cmdline = ' '.join(proc.info['cmdline'] or [])
            if 'clawteam' in cmdline and '--session' in cmdline:
                # Extract team and agent name from session
                session = None
                for part in proc.info['cmdline'] or []:
                    if '--session' in part:
                        idx = proc.info['cmdline'].index(part)
                        if idx + 1 < len(proc.info['cmdline']):
                            session = proc.info['cmdline'][idx + 1]
                            break
                
                if session and 'clawteam-' in session:
                    parts = session.split('-')
                    if len(parts) >= 3:
                        agent_team = '-'.join(parts[1:-1])  # team name
                        agent_name = parts[-1]  # agent name
                        
                        if team_name is None or agent_team == team_name:
                            agents.append({
                                'pid': proc.info['pid'],
                                'team': agent_team,
                                'agent': agent_name,
                                'session': session,
                                'start_time': datetime.fromtimestamp(proc.info['create_time']),
                                'cmdline': cmdline[:200],  # Truncate for display
                            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    return agents


def get_idle_duration(pid: int) -> Optional[float]:
    """Get how long a process has been idle (in minutes).
    
    Returns None if cannot determine.
    """
    try:
        proc = psutil.Process(pid)
        # Check CPU usage over last interval
        cpu_percent = proc.cpu_percent(interval=0.1)
        
        # If CPU usage is very low, consider it idle
        # Get last activity time from process info
        io_counters = proc.io_counters()
        if io_counters:
            # Use read_bytes as proxy for activity
            # This is a simplification - in production would track actual activity
            return 0  # Active
        
        return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_process_idle(pid: int, idle_threshold_minutes: int = 30) -> bool:
    """Check if a process has been idle for longer than threshold."""
    try:
        proc = psutil.Process(pid)
        
        # Get process CPU times
        cpu_times = proc.cpu_times()
        total_cpu = cpu_times.user + cpu_times.system
        
        # Get process runtime
        create_time = datetime.fromtimestamp(proc.create_time())
        runtime_minutes = (datetime.now() - create_time).total_seconds() / 60
        
        # If process has been running for a while with very low CPU usage
        if runtime_minutes > idle_threshold_minutes:
            # Check recent CPU usage
            cpu_percent = proc.cpu_percent(interval=0.5)
            if cpu_percent < 1.0:  # Less than 1% CPU usage
                return True
        
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True  # Process doesn't exist, consider it idle


def kill_agent_process(pid: int, graceful: bool = True) -> bool:
    """Kill an agent process.
    
    Args:
        pid: Process ID to kill
        graceful: If True, send SIGTERM first, then SIGKILL
    
    Returns:
        True if process was killed, False otherwise
    """
    try:
        proc = psutil.Process(pid)
        
        if graceful:
            # Try graceful shutdown first
            proc.terminate()
            try:
                proc.wait(timeout=5)
                return True
            except psutil.TimeoutExpired:
                # Force kill if graceful shutdown fails
                proc.kill()
                proc.wait(timeout=2)
                return True
        else:
            # Force kill immediately
            proc.kill()
            proc.wait(timeout=2)
            return True
            
    except psutil.NoSuchProcess:
        return True  # Already dead
    except Exception:
        return False


def cleanup_team_agents(team_name: str, idle_only: bool = True, 
                        idle_threshold_minutes: int = 30,
                        dry_run: bool = False) -> dict:
    """Cleanup agents for a specific team.
    
    Args:
        team_name: Name of the team to cleanup
        idle_only: If True, only kill idle agents
        idle_threshold_minutes: How long before considering an agent idle
        dry_run: If True, only report what would be done
    
    Returns:
        Dict with cleanup results
    """
    agents = get_agent_processes(team_name)
    results = {
        'team': team_name,
        'total_agents': len(agents),
        'killed': [],
        'skipped': [],
        'errors': [],
    }
    
    for agent in agents:
        pid = agent['pid']
        agent_name = agent['agent']
        
        should_kill = True
        
        if idle_only:
            # Check if agent is idle
            if not is_process_idle(pid, idle_threshold_minutes):
                should_kill = False
                results['skipped'].append({
                    'pid': pid,
                    'agent': agent_name,
                    'reason': 'not_idle'
                })
        
        if should_kill:
            if dry_run:
                results['killed'].append({
                    'pid': pid,
                    'agent': agent_name,
                    'dry_run': True
                })
            else:
                if kill_agent_process(pid, graceful=True):
                    results['killed'].append({
                        'pid': pid,
                        'agent': agent_name,
                        'dry_run': False
                    })
                else:
                    results['errors'].append({
                        'pid': pid,
                        'agent': agent_name,
                        'error': 'failed_to_kill'
                    })
    
    return results


def cleanup_all_teams(idle_only: bool = True, 
                     idle_threshold_minutes: int = 30,
                     dry_run: bool = False) -> List[dict]:
    """Cleanup agents for all teams.
    
    Returns list of cleanup results per team.
    """
    # Get all unique team names from running agents
    agents = get_agent_processes()
    teams = set(a['team'] for a in agents)
    
    results = []
    for team_name in teams:
        result = cleanup_team_agents(
            team_name, 
            idle_only=idle_only,
            idle_threshold_minutes=idle_threshold_minutes,
            dry_run=dry_run
        )
        results.append(result)
    
    return results


def get_cleanup_summary() -> dict:
    """Get summary of current agents that could be cleaned up."""
    agents = get_agent_processes()
    
    summary = {
        'total_agents': len(agents),
        'teams': {},
        'idle_agents': [],
    }
    
    for agent in agents:
        team = agent['team']
        if team not in summary['teams']:
            summary['teams'][team] = []
        summary['teams'][team].append(agent)
        
        # Check if idle
        if is_process_idle(agent['pid']):
            summary['idle_agents'].append(agent)
    
    return summary
