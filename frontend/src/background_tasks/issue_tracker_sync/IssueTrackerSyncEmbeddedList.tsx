import { Chip, Stack, Tooltip } from "@mui/material";
import {
    AutocompleteInput,
    ChipField,
    Datagrid,
    DateField,
    FilterForm,
    FunctionField,
    ListContextProvider,
    NumberInput,
    ResourceContextProvider,
    TextField,
    useListController,
} from "react-admin";

import { getSettingListSize, getSettingRowsPerPage } from "../../access_control/users/functions";
import { CustomPagination } from "../../commons/custom_fields/CustomPagination";
import IssueTrackerSyncRetry from "./IssueTrackerSyncRetry";
import {
    ISSUE_TRACKER_DESIRED_STATE_CHOICES,
    ISSUE_TRACKER_SYNC_STATE_CHOICES,
} from "./types";

const STATE_COLORS: Record<string, string> = {
    PENDING: "warning",
    IN_PROGRESS: "info",
    SUCCEEDED: "success",
    FAILED: "error",
};

const IssueTrackerSyncEmbeddedList = () => {
    const listContext = useListController({
        filter: {},
        perPage: getSettingRowsPerPage(),
        resource: "issue_tracker_statuses",
        sort: { field: "id", order: "DESC" },
        disableSyncWithLocation: false,
        storeKey: "issue_tracker_statuses.embedded",
    });

    if (listContext.isLoading) {
        return <div>Loading...</div>;
    }

    const listFilters = [
        <NumberInput key="product-filter" source="product" label="Product id" alwaysOn sx={{ width: 140 }} />,
        <AutocompleteInput
            key="sync-state-filter"
            source="sync_state"
            choices={ISSUE_TRACKER_SYNC_STATE_CHOICES}
            alwaysOn
        />,
        <AutocompleteInput
            key="desired-state-filter"
            source="desired_state"
            choices={ISSUE_TRACKER_DESIRED_STATE_CHOICES}
            alwaysOn
        />,
    ];

    return (
        <ResourceContextProvider value="issue_tracker_statuses">
            <ListContextProvider value={listContext}>
                <div style={{ width: "100%" }}>
                    <Stack
                        direction="row"
                        sx={{
                            alignItems: "center",
                            marginBottom: 1,
                            "& form": { flex: "0 1 auto", minHeight: "unset", paddingBottom: 0 },
                            "& form .RaFilterForm-filterFormInput .MuiFormControl-root": { marginTop: 0 },
                        }}
                    >
                        <FilterForm filters={listFilters} />
                    </Stack>
                    <Datagrid
                        size={getSettingListSize()}
                        rowClick={false}
                        bulkActionButtons={false}
                        resource="issue_tracker_statuses"
                    >
                        <TextField source="id" sortable={false} />
                        <TextField source="product_name" label="Product" sortable={false} />
                        <TextField source="observation" label="Observation id" sortable={false} />
                        <TextField source="issue_id" label="Issue id" sortable={false} />
                        <ChipField
                            source="desired_state"
                            label="Desired"
                            sortable={false}
                            color="primary"
                            size="small"
                            variant="outlined"
                        />
                        <FunctionField
                            source="sync_state"
                            label="State"
                            render={(record) => (
                                <Chip
                                    label={record.sync_state}
                                    color={(STATE_COLORS[record.sync_state] as any) || "default"}
                                    size="small"
                                    variant="outlined"
                                />
                            )}
                        />
                        <DateField source="next_attempt_at" label="Next attempt" showTime sortable={false} />
                        <FunctionField
                            source="last_error"
                            sortable={false}
                            render={(record) =>
                                record.last_error ? (
                                    <Tooltip title={record.last_error}>
                                        <span>
                                            {record.last_error.length > 60
                                                ? `${record.last_error.slice(0, 60)}...`
                                                : record.last_error}
                                        </span>
                                    </Tooltip>
                                ) : (
                                    <span />
                                )
                            }
                        />
                        <FunctionField
                            source="retry"
                            label=""
                            sortable={false}
                            render={(record) => <IssueTrackerSyncRetry record={record} />}
                        />
                    </Datagrid>
                    <CustomPagination />
                </div>
            </ListContextProvider>
        </ResourceContextProvider>
    );
};

export default IssueTrackerSyncEmbeddedList;
