import { useEffect, useRef, useState, type ReactNode } from "react";

import type { TeamState } from "../types";
import { HandoffDialog } from "./HandoffDialog";
import { PresenceDialog } from "./PresenceDialog";
import { fetchTeamStatus, releaseReview, takeReview } from "./teamApi";
import { awayLine, claimSuffix, firstName, needsPresenceDialog } from "./teamCopy";
import { queueEntryToast } from "./useReviewPresence";

export interface TeamRow {
  id: string;
  title: string;
  team?: TeamState;
}

/**
 * Opening a review a teammate is in, taking it, handing it off (SPEC 4.16): the behaviour shared by every list that
 * shows rows with a `team` (the hosted Inbox today, Home where it lists a team's reviews). The list keeps the rows; this
 * asks the server, reports the result and draws the two dialogs. Nothing here runs for a row without `team`, which is
 * every row on a local install.
 */
/** The server could not move the review request (the handoff or the take stands): say so, with its reason. */
function notMoved(team: TeamState): string {
  return ` \u00b7 The review request did not move${team.request_move_error ? `: ${team.request_move_error}` : ""}`;
}

export function useTeamRowActions<R extends TeamRow>({ rows, now, setTeam, open, toast, announce, focusRow, another }: {
  rows: readonly R[];
  now: number;
  /** Put the server's answer on the row. */
  setTeam: (id: string, team: TeamState) => void;
  /** Open the review (the list navigates). */
  open: (row: R) => void;
  toast: (toast: { text: string; tone?: "alert" }) => void;
  announce?: (text: string) => void;
  /** Give the row focus again after its actions changed. */
  focusRow: (id: string) => void;
  /** "Pick another": move on to a review nobody is in. */
  another: (row: R) => void;
}): {
  asksFirst: (row: R) => boolean;
  openRow: (row: R) => void;
  take: (row: R) => void;
  release: (row: R) => void;
  handOff: (row: R) => void;
  element: ReactNode;
} {
  const [presenceFor, setPresenceFor] = useState("");
  const [handoffFor, setHandoffFor] = useState("");
  // Hand off opened from the presence dialog has no opener to go back to: Esc returns to the row.
  const handoffFromPresence = useRef(false);
  const together = useRef(new Set<string>());
  const byId = (id: string) => rows.find((row) => row.id === id);

  const asksFirst = (row: R) => needsPresenceDialog(row.team) && !together.current.has(row.id);
  const openRow = (row: R) => {
    if (asksFirst(row)) { setPresenceFor(row.id); return; }
    open(row);
  };
  const say = (text: string, tone?: "alert") => {
    toast({ text, tone });
    if (!tone) announce?.(text);
  };
  const take = async (row: R) => {
    setPresenceFor("");
    try {
      const team = await takeReview(row.id);
      setTeam(row.id, team);
      focusRow(row.id);
      const taken = `Taken by you${team.claim ? claimSuffix(team.claim) : ""}`;
      if (team.request_moved === false) say(`${taken}${notMoved(team)}`, "alert");
      else say(taken);
    } catch (reason) {
      say(reason instanceof Error && reason.message ? reason.message : "Couldn't take this review · try again", "alert");
    }
  };
  const release = async (row: R) => {
    try {
      setTeam(row.id, await releaseReview(row.id));
      focusRow(row.id);
      say("You let go of this review");
    } catch (reason) {
      say(reason instanceof Error && reason.message ? reason.message : "Couldn't let go of this review \u00b7 try again", "alert");
    }
  };
  const handedOff = (row: R, team: TeamState) => {
    setHandoffFor("");
    setTeam(row.id, team);
    focusRow(row.id);
    const given = team.handoff;
    const away = given?.away ? awayLine(given.away.name, given.away.until, given.away.rerouted === false ? null : given.to.name, Date.now()) : "";
    const handed = `Handed off to ${given?.to.name ?? "your teammate"}${away ? ` · ${away}` : ""}`;
    if (team.request_moved === false) say(`${handed}${notMoved(team)}`, "alert");
    else say(handed);
  };

  // The dialog tells what the person in it has sent, which the list's row does not carry: read it as the dialog opens.
  // Nobody in it any more means there is nothing to ask about, so the review opens.
  const [fresh, setFresh] = useState<{ id: string; team: TeamState } | null>(null);
  const latest = useRef({ open, byId });
  latest.current = { open, byId };
  useEffect(() => {
    if (!presenceFor) { setFresh(null); return; }
    let live = true;
    fetchTeamStatus(presenceFor)
      .then((team) => {
        if (!live) return;
        const row = latest.current.byId(presenceFor);
        if (team.reviewing.length === 0 && !team.claim && !team.handoff && row) { setPresenceFor(""); latest.current.open(row); return; }
        setFresh({ id: presenceFor, team });
      })
      .catch(() => undefined);
    return () => { live = false; };
  }, [presenceFor]);
  const presenceRow = presenceFor ? byId(presenceFor) : undefined;
  const presenceTeam = fresh?.id === presenceFor && fresh.team.reviewing.length > 0 ? fresh.team : presenceRow?.team;
  const handoffRow = handoffFor ? byId(handoffFor) : undefined;
  const element = (
    <>
      {presenceRow && presenceTeam ? (
        <PresenceDialog
          team={presenceTeam}
          now={now}
          onTogether={() => {
            together.current.add(presenceRow.id);
            setPresenceFor("");
            // The route change clears a toast shown here, so the review screen shows it.
            queueEntryToast(presenceRow.id, `Reviewing together with ${firstName(presenceTeam.reviewing[0]?.name ?? "")} · notes stay private until sent`);
            open(presenceRow);
          }}
          onAnyway={() => { together.current.add(presenceRow.id); setPresenceFor(""); open(presenceRow); }}
          onOther={() => { setPresenceFor(""); another(presenceRow); }}
          onTake={() => void take(presenceRow)}
          onHandOff={() => { handoffFromPresence.current = true; setHandoffFor(presenceRow.id); setPresenceFor(""); }}
          onClose={() => setPresenceFor("")}
        />
      ) : null}
      {handoffRow ? (
        <HandoffDialog
          reviewId={handoffRow.id}
          now={now}
          onDone={(team) => handedOff(handoffRow, team)}
          onClose={() => setHandoffFor("")}
          onCloseAutoFocus={(event) => {
            if (!handoffFromPresence.current) return;
            handoffFromPresence.current = false;
            event.preventDefault();
            focusRow(handoffRow.id);
          }}
        />
      ) : null}
    </>
  );
  return { asksFirst, openRow, take: (row) => void take(row), release: (row) => void release(row), handOff: (row) => setHandoffFor(row.id), element };
}
