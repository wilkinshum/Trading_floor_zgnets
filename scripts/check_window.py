import yaml
from datetime import datetime
from zoneinfo import ZoneInfo

cfg = yaml.safe_load(open("configs/workflow.yaml"))
now = datetime.now(ZoneInfo("America/New_York"))
end_h, end_m = cfg["hours"]["end"].split(":")
end_time = now.replace(hour=int(end_h), minute=int(end_m), second=0)
start_h, start_m = cfg["hours"]["start"].split(":")
start_time = now.replace(hour=int(start_h), minute=int(start_m), second=0)
within = start_time <= now <= end_time
print(f"Now: {now.strftime('%H:%M')}, Window: {cfg['hours']['start']}-{cfg['hours']['end']}, Within: {within}")
