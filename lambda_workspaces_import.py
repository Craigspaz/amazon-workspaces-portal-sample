#!/usr/bin/python

#
# Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

import boto3
import os
import logging
import time
import json
from botocore.exceptions import ClientError,EndpointConnectionError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DDBTableName = os.environ.get("DynamoDBTableName", "WorkspacesPortal")

RegistrationCodes = {}

def GetRegCode(Client, DirectoryId):
    if DirectoryId in RegistrationCodes: return(RegistrationCodes[DirectoryId])
    
    try:
        DirectoryList = Client.describe_workspace_directories()
    except Exception as e:
        logger.error("Did not get list of directories: "+str(e))
        return("")
        
    for Dir in DirectoryList["Directories"]:
        RegistrationCodes[Dir["DirectoryId"]] = Dir["RegistrationCode"]

    if DirectoryId in RegistrationCodes: return(RegistrationCodes[DirectoryId])

    return("")
        
def lambda_handler(event, context):
    Regions = []
    if os.environ.get("REGIONLIST") is not None:
        Regions = os.environ.get("REGIONLIST").split(",")
        logger.info("Regions: "+",".join(Regions))
    else:
        try:
            EC2 = boto3.client("ec2")
            Response = EC2.describe_regions()
            for Region in Response["Regions"]:
                Regions.append(Region["RegionName"])
        except Exception as e:
            logger.error("Unable to get a list of regions: "+str(e))
            Regions.append("us-east-1")
        logger.info("All regions: "+"".join(Regions))

    for TargetRegion in Regions:
        logger.info("Checking: "+TargetRegion)
        WorkspacesClient = boto3.client("workspaces", region_name=TargetRegion)
        paginator = WorkspacesClient.get_paginator("describe_workspaces")
     
        try:
            ListResponse = {}
            for page in paginator.paginate(PaginationConfig={"PageSize": 25}):
                if ListResponse:
                    ListResponse["Workspaces"] = (
                        ListResponse["Workspaces"] + page["Workspaces"]
                    )
                else:
                    ListResponse = {**ListResponse, **page}
            logger.debug(
                "ListResponse  %s, size %s",
                ListResponse["Workspaces"],
                len(ListResponse["Workspaces"]),
            )
            logger.info("Found %s workspaces", len(ListResponse["Workspaces"]))
        except EndpointConnectionError as e:
            logger.warning("Could not connect to endpoint in region "+TargetRegion)
            continue
        except Exception as e:
            logger.error("Failed to get Workspaces list for region "+TargetRegion+" - "+str(e))
            continue
            
        if len(ListResponse["Workspaces"]) == 0:
            logger.info("  No Workspaces instances found in region "+TargetRegion)
            continue

        #
        # Here we get the connection details for all the Workspaces instances at once
        # It is more efficient this way even though we could call this API
        # individually for each instance we have in ListResponse
        #
        Results = WorkspacesClient.describe_workspaces_connection_status()
        ConnectionResponse = Results
        while Results.get("NextToken"):
            Results = WorkspacesClient.describe_workspaces_connection_status(
                NextToken=Results["NextToken"]
            )
            ConnectionResponse["WorkspacesConnectionStatus"] = (
                ConnectionResponse["WorkspacesConnectionStatus"]
                + Results["WorkspacesConnectionStatus"]
            )
        logger.info(
            "Found %s workspaces_connection_status",
            len(ConnectionResponse["WorkspacesConnectionStatus"]),
        )
        LastConnectedTime = {}
        for Connection in ConnectionResponse["WorkspacesConnectionStatus"]:
            try:
                LastConnectedTime[Connection["WorkspaceId"]] = Connection["LastKnownUserConnectionTimestamp"].strftime("%s")
            except:
                pass

        DynamoDBClient = boto3.client("dynamodb")
        for Instance in ListResponse["Workspaces"]:
            logger.info("  WorkspaceId: "+Instance["WorkspaceId"])

            Item = {"WorkspaceId":  {"S":Instance["WorkspaceId"]},
                    "UserName":     {"S":Instance["UserName"]},
                    "Region":       {"S":TargetRegion},
                    "InstanceState":{"S":Instance["State"]},
                    "LastTouched":  {"N":str(time.time())},
                    "RunningMode":  {"S":Instance["WorkspaceProperties"]["RunningMode"]},
                    "RegCode":      {"S":GetRegCode(WorkspacesClient, Instance["DirectoryId"])}
            }

            if "ComputerName"          in Instance:          Item["ComputerName"]  = {"S":Instance["ComputerName"]}
            if "IpAddress"             in Instance:          Item["IPAddress"]     = {"S":Instance["IpAddress"]}
            if Instance["WorkspaceId"] in LastConnectedTime: Item["LastConnected"] = {"N":LastConnectedTime[Instance["WorkspaceId"]]}
            logger.debug(
                "  WorkspaceId: " + Instance["WorkspaceId"] + " " + json.dumps(Item)
            )
            try:
                DynamoDBClient.put_item(TableName=DDBTableName, Item=Item)
            except ClientError as e:
                logger.error("DynamoDB error: "+e.response["Error"]["Message"])