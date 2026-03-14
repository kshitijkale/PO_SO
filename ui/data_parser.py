import json
import os
from pathlib import Path
from typing import Any

import pandas as pd


class LogParser:
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)

    def load_jsonl_to_df(self, filename: str) -> pd.DataFrame:
        file_path = self.run_dir / filename
        if not file_path.exists():
            return pd.DataFrame()
        
        data = []
        with open(file_path, "r") as f:
            for line in f:
                if line.strip():
                    data.append(json.loads(line))
        return pd.DataFrame(data)

    def get_summary(self) -> pd.DataFrame:
        return self.load_jsonl_to_df("summary.jsonl")

    def get_events_for_iteration(self, iteration: int) -> list[dict[str, Any]]:
        file_path = self.run_dir / "log.jsonl"
        if not file_path.exists():
            return []
            
        events = []
        with open(file_path, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        if record.get("iteration") == iteration:
                            events.append(record)
                    except json.JSONDecodeError:
                        continue
        return events

    def get_lessons(self) -> pd.DataFrame:
        return self.load_jsonl_to_df("memory/lesson_events.jsonl")

    def get_memory_events(self) -> pd.DataFrame:
        return self.load_jsonl_to_df("memory/memory_events.jsonl")

    def list_llm_calls(self) -> list[dict[str, Any]]:
        llm_dir = self.run_dir / "llm_calls"
        if not llm_dir.exists() or not llm_dir.is_dir():
            return []
            
        calls = []
        for file in llm_dir.glob("*.json"):
            with open(file, "r") as f:
                try:
                    data = json.load(f)
                    calls.append({
                        "file": file.name,
                        "iteration": data.get("iteration"),
                        "component_name": data.get("component_name"),
                        "model_id": data.get("model_id"),
                        "latency_ms": data.get("latency_ms"),
                        "data": data
                    })
                except json.JSONDecodeError:
                    continue
        return calls

    def get_llm_call_details(self, iteration: int, component: str) -> dict[str, str]:
        llm_dir = self.run_dir / "llm_calls"
        prefix = f"iter_{iteration:03d}_{component}"
        
        prompt_path = llm_dir / f"{prefix}_prompt.txt"
        response_path = llm_dir / f"{prefix}_response.txt"
        
        prompt_text = ""
        if prompt_path.exists():
            prompt_text = prompt_path.read_text(encoding="utf-8")
            
        response_text = ""
        if response_path.exists():
            response_text = response_path.read_text(encoding="utf-8")
            
        return {"prompt": prompt_text, "response": response_text}

    def list_candidates(self) -> list[dict[str, Any]]:
        cand_dir = self.run_dir / "candidates"
        if not cand_dir.exists() or not cand_dir.is_dir():
            return []
            
        candidates = []
        for file in cand_dir.glob("*.json"):
            with open(file, "r") as f:
                try:
                    data = json.load(f)
                    candidates.append(data)
                except json.JSONDecodeError:
                    continue
        return sorted(candidates, key=lambda x: x.get("index", 0))

def get_available_runs(base_dir: str = "outputs") -> list[str]:
    base_path = Path(base_dir)
    if not base_path.exists():
        return []
        
    runs = []
    for item in base_path.iterdir():
        if item.is_dir():
            # Check if it has summary.jsonl or log.jsonl to confirm it's a valid run
            if (item / "summary.jsonl").exists() or (item / "log.jsonl").exists():
                runs.append(item.name)
    return sorted(runs)
