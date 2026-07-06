import signal
import tkinter as tk
from tkinter import ttk

from core.race_controller import PHASES, CLEAR_ACTION

ROW_LABELS = [("Run", "run"), ("Team", "team")] + PHASES


class ControlUI(tk.Tk):
    def __init__(self, race_controller, teams):
        super().__init__()
        self.title("eISR Livestream Control")
        self.resizable(False, False)

        self.race_controller = race_controller
        self.teams = teams
        self.race_controller.add_listener(self._on_run_update)

        self._build_run_number_panel()
        self._build_team_panel()
        self._build_phase_panel()
        self._build_clear_panel()
        self._build_run_times_panel()

    def _build_run_number_panel(self):
        frame = ttk.LabelFrame(self, text="Run Number", padding=10)
        frame.pack(fill="x", padx=10, pady=10)

        self.run_number_var = tk.StringVar(value="-")
        self.run_number_var.trace_add(
            "write", lambda *_: self.race_controller.set_run_number(self.run_number_var.get())
        )

        vcmd = (self.register(self._validate_run_number), "%P")
        entry = ttk.Entry(frame, textvariable=self.run_number_var, width=10, validate="key", validatecommand=vcmd)
        entry.pack(side="left")
        ttk.Label(frame, text="  (integer, or \"-\" for a test run)").pack(side="left")

    def _validate_run_number(self, new_value):
        return new_value == "" or new_value == "-" or new_value.isdigit()

    def _build_team_panel(self):
        frame = ttk.LabelFrame(self, text="Team Overlay", padding=10)
        frame.pack(fill="x", padx=10, pady=(0, 10))

        team_ids = list(self.teams.keys())
        labels = [f"{self.teams[tid].get('teamName', tid)} ({tid})" for tid in team_ids]
        self._label_to_id = dict(zip(labels, team_ids))

        self.team_var = tk.StringVar()
        combo = ttk.Combobox(
            frame, textvariable=self.team_var, values=labels, state="readonly", width=40
        )
        combo.pack(side="left", padx=(0, 10))
        if labels:
            combo.current(0)

        ttk.Button(frame, text="Load Team", command=self._on_load_team).pack(side="left")

    def _on_load_team(self):
        team_id = self._label_to_id.get(self.team_var.get())
        if team_id:
            team_label = self.teams.get(team_id, {}).get("teamName", team_id)
            self.race_controller.set_team(team_id, team_label)

    def _build_phase_panel(self):
        frame = ttk.LabelFrame(self, text="Race Phase", padding=10)
        frame.pack(fill="x", padx=10, pady=(0, 10))

        for i, (label, action) in enumerate(PHASES):
            btn = ttk.Button(
                frame, text=label,
                command=lambda a=action: self.race_controller.trigger_phase(a),
            )
            btn.grid(row=i // 4, column=i % 4, padx=5, pady=5, sticky="ew")

        for col in range(4):
            frame.columnconfigure(col, weight=1)

    def _build_clear_panel(self):
        # Kept on its own layer, visually distinct (colour + separator) from
        # the phase grid above so it isn't mistaken for another phase button.
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=(4, 0))

        frame = ttk.LabelFrame(self, text="Reset", padding=10)
        frame.pack(fill="x", padx=10, pady=10)

        label, action = CLEAR_ACTION
        clear_btn = tk.Button(
            frame, text=label,
            command=lambda: self.race_controller.trigger_phase(action),
            bg="#c0392b", fg="white", activebackground="#992d22", activeforeground="white",
            font=("TkDefaultFont", 11, "bold"), padx=12, pady=8, relief="raised",
        )
        clear_btn.pack(fill="x")

    def _build_run_times_panel(self):
        # Shows the run currently in progress, updating live as each phase
        # button is pressed. After "clear" it keeps showing the just-finished
        # run until the next "toSet" starts filling in a fresh one. One value
        # per line so a block of rows can be selected and pasted straight
        # into a spreadsheet column.
        frame = ttk.LabelFrame(self, text="Run Times", padding=10)
        frame.pack(fill="x", padx=10, pady=(0, 10))

        self.run_times_text = tk.Text(frame, height=len(ROW_LABELS) + 1, width=30, state="disabled")
        self.run_times_text.pack(fill="both", expand=True)

    def _on_run_update(self, run, _action):
        # May be called from the Launch Control's background thread - hop
        # back onto the Tk main thread before touching any widgets. The
        # action itself isn't needed here (the run-times panel just
        # re-renders the whole run dict) but is part of RaceController's
        # listener signature.
        self.after(0, self._render_run_times, run)

    def _render_run_times(self, run):
        self.run_times_text.configure(state="normal")
        self.run_times_text.delete("1.0", "end")
        if not run:
            self.run_times_text.insert("end", "No runs recorded yet.")
        else:
            lines = [f"{label}: {run[key]}" for label, key in ROW_LABELS if key in run]
            self.run_times_text.insert("end", "\n".join(lines))
        self.run_times_text.configure(state="disabled")


def run(race_controller, teams, on_exit=None):
    app = ControlUI(race_controller, teams)

    # Tk's event loop can otherwise sit inside a blocking C call and not
    # notice a SIGINT for a while - this periodic no-op hands control back
    # to Python often enough that Ctrl+C is picked up promptly.
    def _pump():
        app.after(200, _pump)

    _pump()
    signal.signal(signal.SIGINT, lambda signum, frame: app.destroy())

    try:
        app.mainloop()
    finally:
        if on_exit:
            on_exit()
