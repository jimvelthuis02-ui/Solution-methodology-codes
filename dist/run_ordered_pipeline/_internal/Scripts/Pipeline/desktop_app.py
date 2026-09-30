from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_SCRIPT = PROJECT_ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "Input files"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "Output"

REQUIRED_INPUT_FILES = [
    DEFAULT_INPUT_DIR / "Locations" / "Location details.xlsx",
    DEFAULT_INPUT_DIR / "Locations" / "SKU_Location_Detail.csv",
    DEFAULT_INPUT_DIR / "WSM_Weights.csv",
]


class PipelineDesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Warehouse Slot Sizing Model")
        self.root.geometry("840x620")
        self.root.minsize(760, 520)

        self.input_dir = tk.StringVar(value=str(DEFAULT_INPUT_DIR))
        self.output_dir = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.current_stage = tk.StringVar(value="Idle")
        self.status_text = tk.StringVar(value="Waiting for input validation.")
        self.process: subprocess.Popen[str] | None = None
        self._log_buffer: list[str] = []

        self._build_ui()
        self._validate_inputs()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Warehouse Slot Sizing Model", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 12))

        folder_frame = ttk.LabelFrame(main, text="Folders", padding=12)
        folder_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(folder_frame, text="Input folder:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        input_entry = ttk.Entry(folder_frame, textvariable=self.input_dir)
        input_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(0, 8))
        ttk.Button(folder_frame, text="Browse", command=self._browse_input).grid(row=0, column=2, pady=(0, 8))

        ttk.Label(folder_frame, text="Output folder:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        output_entry = ttk.Entry(folder_frame, textvariable=self.output_dir)
        output_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 8))
        ttk.Button(folder_frame, text="Browse", command=self._browse_output).grid(row=1, column=2, pady=(0, 8))
        folder_frame.columnconfigure(1, weight=1)

        ttk.Label(main, textvariable=self.status_text, foreground="#1f6feb").pack(anchor="w", pady=(0, 8))

        progress_frame = ttk.LabelFrame(main, text="Progress", padding=12)
        progress_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(progress_frame, text="Current stage:").pack(anchor="w")
        ttk.Label(progress_frame, textvariable=self.current_stage, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(main, text="Run log", padding=12)
        log_frame.pack(fill="both", expand=True)

        self.log_box = tk.Text(log_frame, height=16, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Run Model", command=self._run_pipeline).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Refresh validation", command=self._validate_inputs).pack(side="left")

    def _browse_input(self) -> None:
        folder = filedialog.askdirectory(title="Select Input Folder")
        if folder:
            self.input_dir.set(folder)
            self._validate_inputs()

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            self.output_dir.set(folder)
            self.status_text.set(f"Output folder selected: {folder}")

    def _validate_inputs(self) -> None:
        input_dir = Path(self.input_dir.get()).expanduser().resolve()
        required_files = [
            input_dir / "Locations" / "Location details.xlsx",
            input_dir / "Locations" / "SKU_Location_Detail.csv",
            input_dir / "WSM_Weights.csv",
        ]
        missing = [str(path) for path in required_files if not path.exists()]

        if not input_dir.exists():
            self.status_text.set("Input folder does not exist.")
            self.current_stage.set("Validation failed")
            self._append_log("Validation failed: input folder missing.\n")
            return

        if missing:
            self.status_text.set("Input validation failed. Missing required files.")
            self.current_stage.set("Validation failed")
            self._append_log("Missing required files:\n" + "\n".join(f"- {item}" for item in missing) + "\n")
            return

        self.status_text.set("Input validation successful.")
        self.current_stage.set("Ready to run")
        self._append_log("Input validation successful.\n")

    def _append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _run_pipeline(self) -> None:
        input_dir = Path(self.input_dir.get()).expanduser().resolve()
        output_dir = Path(self.output_dir.get()).expanduser().resolve()

        if not input_dir.exists():
            messagebox.showerror("Invalid input folder", "The selected input folder does not exist.")
            return

        missing = [path for path in REQUIRED_INPUT_FILES if not path.exists()]
        if missing:
            messagebox.showerror("Missing required inputs", "The model cannot run until all required input files are available.")
            return

        output_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PIPELINE_PROJECT_ROOT"] = str(PROJECT_ROOT)
        env["PIPELINE_INPUT_DIR"] = str(input_dir)
        env["PIPELINE_OUTPUT_DIR"] = str(output_dir)

        self.current_stage.set("Launching pipeline...")
        self.status_text.set("Running model...")
        self._append_log(f"Launching pipeline from: {PROJECT_ROOT}\n")
        self._append_log(f"Input folder: {input_dir}\n")
        self._append_log(f"Output folder: {output_dir}\n")

        try:
            self.process = subprocess.Popen(
                [sys.executable, str(PIPELINE_SCRIPT)],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.current_stage.set("Launch failed")
            self.status_text.set(f"Failed to start pipeline: {exc}")
            messagebox.showerror("Launch failed", str(exc))
            return

        threading.Thread(target=self._read_process_output, daemon=True).start()

    def _read_process_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return

        for line in iter(self.process.stdout.readline, ""):
            cleaned = line.rstrip()
            if not cleaned:
                continue
            self._append_log(cleaned + "\n")
            if cleaned.startswith("Running:"):
                self.current_stage.set(cleaned.replace("Running:", "").strip())
            elif "Ordered pipeline complete" in cleaned:
                self.current_stage.set("Pipeline complete")
                self.status_text.set("Pipeline completed successfully.")
                self.root.after(0, lambda: messagebox.showinfo("Success", f"The model completed successfully.\nOutput folder: {self.output_dir.get()}\nPower BI can now be refreshed."))
            elif "Traceback" in cleaned or "ERROR" in cleaned.upper() or "FileNotFoundError" in cleaned:
                self.current_stage.set("Pipeline error")
                self.status_text.set("The model finished with an error.")
                self.root.after(0, lambda: messagebox.showerror("Pipeline error", f"The model encountered an error.\n\n{cleaned}"))

        return_code = self.process.wait()
        if return_code == 0:
            self.status_text.set("Pipeline completed successfully.")
        else:
            self.status_text.set(f"Pipeline failed with exit code {return_code}.")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    root = tk.Tk()
    app = PipelineDesktopApp(root)
    app.run()
