import { Box, Divider, Paper, Tab, Tabs } from "@mui/material";
import { Fragment } from "react";
import { ReactNode } from "react";
import { Link, matchPath, useLocation } from "react-router-dom";

import administration from ".";
import ListHeader from "../../commons/layout/ListHeader";
import issue_tracker_sync from "../issue_tracker_sync";
import IssueTrackerSyncEmbeddedList from "../issue_tracker_sync/IssueTrackerSyncEmbeddedList";
import periodic_tasks from "../periodic_tasks";
import PeriodicTaskEmbeddedList from "../periodic_tasks/PeriodicTaskEmbeddedList";
import statistics from "../statistics";
import BackgroundTasksStatistics from "../statistics/BackgroundTasksStatistics";

function useRouteMatch(patterns: readonly string[]) {
    const { pathname } = useLocation();

    for (const pattern of patterns) {
        const possibleMatch = matchPath(pattern, pathname);
        if (possibleMatch !== null) {
            return possibleMatch;
        }
    }
    return null;
}

interface TabPanelProps {
    children?: ReactNode;
    index: number;
    value: number;
}

function CustomTabPanel(props: TabPanelProps) {
    const { children, value, index, ...other } = props;
    return (
        <div
            role="tabpanel"
            hidden={value !== index}
            id={`simple-tabpanel-${index}`}
            aria-labelledby={`simple-tab-${index}`}
            {...other} // nosemgrep: typescript.react.best-practice.react-props-spreading.react-props-spreading
            // nosemgrep because the props are well defined in the import
        >
            {value === index && <Box sx={{ p: 2 }}>{children}</Box>}
        </div>
    );
}

function a11yProps(index: number) {
    return {
        id: `simple-tab-${index}`,
        "aria-controls": `simple-tabpanel-${index}`,
    };
}

export default function BackgroundTasksAdministration() {
    const routeMatch = useRouteMatch([
        "/background_tasks/statistics",
        "/background_tasks/periodic_tasks",
        "/background_tasks/issue_tracker_sync",
    ]);
    function currentTab(): number {
        switch (routeMatch?.pattern?.path) {
            case "/background_tasks/statistics": {
                return 0;
            }
            case "/background_tasks/periodic_tasks": {
                return 1;
            }
            case "/background_tasks/issue_tracker_sync": {
                return 2;
            }
            default: {
                return 0;
            }
        }
    }

    return (
        <Fragment>
            <ListHeader icon={administration.icon} title="Background Tasks" />
            <Paper sx={{ marginTop: 2 }}>
                <Tabs value={currentTab()} variant="scrollable" scrollButtons="auto">
                    <Tab
                        label="Statistics"
                        icon={<statistics.icon />}
                        to="/background_tasks/statistics"
                        component={Link}
                        {...a11yProps(0)} // nosemgrep: typescript.react.best-practice.react-props-spreading.react-props-spreading
                        // nosemgrep because the props are well defined in the import
                    />
                    <Tab
                        label="Tracked Tasks"
                        icon={<periodic_tasks.icon />}
                        to="/background_tasks/periodic_tasks"
                        component={Link}
                        {...a11yProps(1)} // nosemgrep: typescript.react.best-practice.react-props-spreading.react-props-spreading
                        // nosemgrep because the props are well defined in the import
                    />
                    <Tab
                        label="Issue Tracker"
                        icon={<issue_tracker_sync.icon />}
                        to="/background_tasks/issue_tracker_sync"
                        component={Link}
                        {...a11yProps(2)} // nosemgrep: typescript.react.best-practice.react-props-spreading.react-props-spreading
                        // nosemgrep because the props are well defined in the import
                    />
                </Tabs>
                <Divider />
                <CustomTabPanel value={currentTab()} index={0}>
                    <BackgroundTasksStatistics />
                </CustomTabPanel>
                <CustomTabPanel value={currentTab()} index={1}>
                    <PeriodicTaskEmbeddedList />
                </CustomTabPanel>
                <CustomTabPanel value={currentTab()} index={2}>
                    <IssueTrackerSyncEmbeddedList />
                </CustomTabPanel>
            </Paper>
        </Fragment>
    );
}
