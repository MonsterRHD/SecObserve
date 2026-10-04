import ReplayIcon from "@mui/icons-material/Replay";
import { Button, Tooltip } from "@mui/material";
import { useState } from "react";
import { useNotify, useRefresh } from "react-admin";

import { Spinner } from "../../commons/custom_fields/Spinner";
import { httpClient } from "../../commons/ra-data-django-rest-framework";

type IssueTrackerSyncRetryProps = {
    record: any;
};

const IssueTrackerSyncRetry = ({ record }: IssueTrackerSyncRetryProps) => {
    const [loading, setLoading] = useState(false);
    const refresh = useRefresh();
    const notify = useNotify();

    if (record.sync_state !== "FAILED" && record.sync_state !== "PENDING") {
        return null;
    }

    const handleClick = () => {
        setLoading(true);
        httpClient(window.__RUNTIME_CONFIG__.API_BASE_URL + `/issue_tracker_statuses/${record.id}/retry/`, {
            method: "POST",
        })
            .then(() => {
                setLoading(false);
                refresh();
                notify("Synchronization enqueued", { type: "success" });
            })
            .catch((error) => {
                setLoading(false);
                notify(error.message, { type: "warning" });
            });
    };

    return (
        <>
            <Tooltip title="Retry synchronization now">
                <span>
                    <Button
                        onClick={handleClick}
                        size="small"
                        startIcon={<ReplayIcon />}
                        sx={{ fontSize: "0.8125rem", whiteSpace: "nowrap" }}
                    >
                        Retry
                    </Button>
                </span>
            </Tooltip>
            <Spinner open={loading} />
        </>
    );
};

export default IssueTrackerSyncRetry;
