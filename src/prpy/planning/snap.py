#!/usr/bin/env python

# Copyright (c) 2013, Carnegie Mellon University
# All rights reserved.
# Authors: Michael Koval <mkoval@cs.cmu.edu>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# - Neither the name of Carnegie Mellon University nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
import numpy
import openravepy
from prpy.util import SetTrajectoryTags
from prpy.planning.base import (
    BasePlanner,
    PlanningError,
    ClonedPlanningMethod,
    Tags
)
from ..collision import SimpleRobotCollisionChecker
from openravepy import CollisionOptions, CollisionOptionsStateSaver


class SnapPlanner(BasePlanner):
    """Planner that checks the straight-line trajectory to the goal.

    SnapPlanner is a utility planner class that collision checks the
    straight-line trajectory to the goal. If that trajectory is invalid,
    e.g. due to an environment or self collision, the planner
    immediately returns failure by raising a PlanningError.

    SnapPlanner is intended to be used only as a "short circuit" to
    speed-up planning between nearby configurations. This planner is
    most commonly used as the first item in a Sequence meta-planner to
    avoid calling a motion planner when the trivial solution is valid.
    """
    def __init__(self, robot_collision_checker=SimpleRobotCollisionChecker):
        super(SnapPlanner, self).__init__()
        self.robot_collision_checker = robot_collision_checker

    def __str__(self):
        return 'SnapPlanner'

    @ClonedPlanningMethod
    def PlanToConfiguration(self, robot, goal, **kw_args):
        """
        Attempt to plan a straight line trajectory from the robot's
        current configuration to the goal configuration. This will
        fail if the straight-line path is not collision free.

        @param robot
        @param goal desired configuration
        @return traj
        """
        return self._Snap(robot, goal, **kw_args)

    @ClonedPlanningMethod
    def PlanToEndEffectorPose(self, robot, goal_pose, **kw_args):
        """
        Attempt to plan a straight line trajectory from the robot's
        current configuration to a desired end-effector pose. This
        happens by finding the closest IK solution to the robot's
        current configuration and attempts to snap there
        (using PlanToConfiguration) if possible.
        In the case of a redundant manipulator, no attempt is
        made to check other IK solutions.

        @param robot
        @param goal_pose desired end-effector pose
        @return traj
        """
        from prpy.planning.exceptions import CollisionPlanningError
        from prpy.planning.exceptions import SelfCollisionPlanningError

        ikp = openravepy.IkParameterizationType
        ikfo = openravepy.IkFilterOptions

        # Find an IK solution. OpenRAVE tries to return a solution that is
        # close to the configuration of the arm, so we don't need to do any
        # custom IK ranking.
        manipulator = robot.GetActiveManipulator()
        ik_param = openravepy.IkParameterization(goal_pose, ikp.Transform6D)
        ik_solution = manipulator.FindIKSolution(
            ik_param, ikfo.CheckEnvCollisions,
            ikreturn=False, releasegil=True
        )

        if ik_solution is None:
            # FindIKSolutions is slower than FindIKSolution,
            # so call this only to identify and raise error when
            # there is no solution
            ik_solutions = manipulator.FindIKSolutions(
                ik_param, ikfo.IgnoreSelfCollisions,
                ikreturn=False, releasegil=True)

            for q in ik_solutions:
                robot.SetActiveDOFValues(q)
                report = openravepy.CollisionReport()
                if self.env.CheckCollision(robot, report=report):
                    raise CollisionPlanningError.FromReport(report, deterministic=True)
                elif robot.CheckSelfCollision(report=report):
                    raise SelfCollisionPlanningError.FromReport(report, deterministic=True)

            raise PlanningError(
                'There is no IK solution at the goal pose.', deterministic=True)

        return self._Snap(robot, ik_solution, **kw_args)

    def _Snap(self, robot, goal, **kw_args):
        from prpy.util import CheckJointLimits
        from prpy.util import GetLinearCollisionCheckPts
        from prpy.planning.exceptions import CollisionPlanningError
        from prpy.planning.exceptions import SelfCollisionPlanningError

        # Create a two-point trajectory between the
        # current configuration and the goal.
        # (a straight line in joint space)
        traj = openravepy.RaveCreateTrajectory(self.env, '')
        cspec = robot.GetActiveConfigurationSpecification('linear')
        active_indices = robot.GetActiveDOFIndices()

        # Check the start position is within joint limits,
        # this can throw a JointLimitError
        start = robot.GetActiveDOFValues()
        CheckJointLimits(robot, start, deterministic=True)

        # Add the start waypoint
        start_waypoint = numpy.zeros(cspec.GetDOF())
        cspec.InsertJointValues(start_waypoint, start, robot,
                                active_indices, False)
        traj.Init(cspec)
        traj.Insert(0, start_waypoint.ravel())

        # Make the trajectory end at the goal configuration, as
        # long as it is not in collision and is not identical to
        # the start configuration.
        CheckJointLimits(robot, goal, deterministic=True)
        if not numpy.allclose(start, goal):
            goal_waypoint = numpy.zeros(cspec.GetDOF())
            cspec.InsertJointValues(goal_waypoint, goal, robot,
                                    active_indices, False)
            traj.Insert(1, goal_waypoint.ravel())

        # Get joint configurations to check
        # Note: this returns a python generator, and if the
        # trajectory only has one waypoint then only the
        # start configuration will be collisioned checked.
        #
        # Sampling function:
        # 'linear'
        # from prpy.util import SampleTimeGenerator
        # linear = SampleTimeGenerator
        # 'Van der Corput'
        from prpy.util import VanDerCorputSampleGenerator
        vdc = VanDerCorputSampleGenerator

        checks = GetLinearCollisionCheckPts(robot, traj,
                                            norm_order=2,
                                            sampling_func=vdc)

        with CollisionOptionsStateSaver(self.env.GetCollisionChecker(),
                                        CollisionOptions.ActiveDOFs):

            # Instantiate a robot checker
            robot_checker = self.robot_collision_checker(robot)

            # Run constraint checks at DOF resolution:
            for t, q in checks:
                # Set the joint positions
                # Note: the planner is using a cloned 'robot' object
                robot.SetActiveDOFValues(q)

                # Check collision (throws an exception on collision)
                robot_checker.VerifyCollisionFree()

        SetTrajectoryTags(traj, {
            Tags.SMOOTH: True,
            Tags.DETERMINISTIC_TRAJECTORY: True,
            Tags.DETERMINISTIC_ENDPOINT: True,
        }, append=True)

        return traj
