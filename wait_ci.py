import urllib.request
import json
import time
import sys

url = 'https://api.github.com/repos/wellnesscoachsriaditya-ux/wellnesscrm-v2-dev/actions/runs?branch=worktree-s1-identity-auth&per_page=1'

while True:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    res = urllib.request.urlopen(req)
    data = json.loads(res.read().decode('utf-8'))
    run = data['workflow_runs'][0]
    
    status = run['status']
    conclusion = run['conclusion']
    
    if conclusion:
        print(f"Commit: {run['head_sha']}")
        print(f"Status: {status}")
        print(f"Conclusion: {conclusion}")
        break
    
    time.sleep(10)
