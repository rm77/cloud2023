# /carrier - the CCRA Cloud Carrier: the transport under the cloud. In the lab that is the
# underlay (br-under, the uplink between PCs, MTU 1600) and the hub's uplink to the internet.
from fastapi import APIRouter, Depends

from ..auth import require_role
from ..infra import cluster as hub_cluster, labhub, nodes as hub_nodes

router = APIRouter(prefix="/carrier", tags=["Carrier"], dependencies=[Depends(require_role("provider", "auditor"))])


@router.get("/underlay", summary="Underlay health: full-MTU path to the hub and node reachability")
def underlay():
    out = dict(hub=labhub.HUB_IP, mtu=hub_cluster.mtu_check())
    try:
        ns = hub_nodes.list()
        out["nodes"] = [dict(name=n["name"], ip=n["ip"], status=n["status"]) for n in ns]
    except labhub.LabError as e:
        out["nodes"] = []
        out["error"] = str(e)
    return out
