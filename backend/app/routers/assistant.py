"""Business Assistant API — local, deterministic, permission-enforced."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, security as S
from ..assistant import engine as E, tools as T, intent as I

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


@router.get("/tools")
def list_tools(db: Session = Depends(get_db),
               user: models.User = Depends(S.require("view"))):
    """The tools THIS user may run — the registry filters by permission."""
    return {"tools": T.available(user), "total_registered": len(T.REGISTRY)}


@router.post("/ask")
def ask(body: dict, db: Session = Depends(get_db),
        user: models.User = Depends(S.require("view"))):
    out = E.ask(db, user, (body or {}).get("q") or "", (body or {}).get("context"))
    S.audit(db, user, "assistant_ask", "assistant", out.get("tool") or "",
            detail=((body or {}).get("q") or "")[:200],
            result=("ok" if out.get("ok") else "denied"))
    return out


@router.post("/run")
def run_tool(body: dict, db: Session = Depends(get_db),
             user: models.User = Depends(S.require("view"))):
    """Run a named tool directly. Same permission enforcement as /ask."""
    name = (body or {}).get("tool") or ""
    args = (body or {}).get("args") or {}
    try:
        data = T.run(name, db, user, **args)
    except T.Denied as e:
        raise HTTPException(403, str(e))
    except T.ToolError as e:
        raise HTTPException(422, str(e))
    S.audit(db, user, "assistant_tool", "assistant", name)
    return {"tool": name, "data": data,
            "warnings": E.rules(name, data, user), "answer": E.summarise(name, data)}


@router.get("/parse")
def parse(q: str = "", db: Session = Depends(get_db),
          user: models.User = Depends(S.require("view"))):
    """Show how a phrase is understood — used by tests and for debugging."""
    return {"query": q, "normalised": I.normalise(q),
            "navigation": I.detect_navigation(q), "period": I.extract_period(q),
            "intent": I.classify(q)}
