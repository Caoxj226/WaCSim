from abc import ABCMeta, abstractmethod

class Control:
    """Defines a control for a PLC to enforce

    :param actuator: actuator that the rule will apply to
    :param action: action that will be performed (OPEN or CLOSED)
    :param value: value that is checked (t0 > value, time == value) etc
    """
    __metaclass__ = ABCMeta

    def __init__(self, actuator, action, value):
        """Constructor method"""
        self.actuator = actuator
        self.action = action
        self.value = value

    @abstractmethod
    def apply(self, generic_plc):
        """
        Applies a control rule using a given PLC.

        :param generic_plc: the PLC that will apply the control actions
        """
        pass


class BelowControl(Control):
    """
    Defines a BELOW control, which takes as a parameter a dependant.

    :param dependant: value that the condition depends on (such as value of tank T0)
    """

    def __init__(self, actuator, action, dependant, value):
        super(BelowControl, self).__init__(actuator, action, value)
        self.dependant = dependant

    def apply(self, generic_plc):
        """Applies the BELOW control rule using a given PLC

        :param generic_plc: the PLC that will apply the control actions
        """
        dep_val = generic_plc.get_tag(self.dependant)
        if dep_val < self.value:
            generic_plc.set_tag(self.actuator, self.action)
            # generic_plc.logger.debug(
            #    generic_plc.intermediate_plc["name"] + " applied " + str(self) +
            #    " because dep_val " + str(dep_val) + ".")
    def applyScadaDecision(self,generic_plc,action):
        """Apply the decision from the SCADA system
        
        Args:
            action: Can be 1.0 (open), 0.0 (closed), or a numeric value 0.0-2.0 for pump speed
        """
        if action == 1.0:
            action = "open"
        elif action == 0.0 or action == 0:
            action = "closed"
        # else: keep numeric value as-is for pump speed control (0.0-2.0)

        generic_plc.set_tag(self.actuator, action)
    def getScadaDecision(self,dependantValue,time):
        """Get a decision based on the control rule in the SCADA system and the dependant value """
        if dependantValue < self.value:
            return self.action

    def applyHybridDecision(self, generic_plc, action, ScadaCommand):
        """Apply the decision from the Hybrid Control system
        
        Args:
            action: Can be 'rule', 'scada', 'open', 'closed', or a numeric value 0.0-2.0 for pump speed
        """
        if action == 'rule':
            self.apply(generic_plc)
        elif action == 'scada':
            self.applyScadaDecision(generic_plc,ScadaCommand)
        elif action == 'open':
            self.applyScadaDecision(generic_plc, 1.0)
        elif action == 'closed':
            self.applyScadaDecision(generic_plc, 0.0)
        else:
            # Numeric value for pump speed control (0.0-2.0)
            self.applyScadaDecision(generic_plc, action)




    def __str__(self):
        return "Control if {dependant} < {value} then set {actuator} to {action}".format(
            dependant=self.dependant, value=self.value, actuator=self.actuator, action=self.action)


class AboveControl(Control):
    """
    Defines a ABOVE control, which takes as a parameter a dependant.

    :param dependant: value that the condition depends on (such as value of tank T0)
    """

    def __init__(self, actuator, action, dependant, value):
        super(AboveControl, self).__init__(actuator, action, value)
        self.dependant = dependant

    def apply(self, generic_plc):
        """
        Applies the ABOVE control rule using a given PLC.

        :param generic_plc: the PLC that will apply the control actions
        """
        dep_val = generic_plc.get_tag(self.dependant)
        if dep_val > self.value:
            generic_plc.set_tag(self.actuator, self.action)
            # generic_plc.logger.debug(
            #     generic_plc.intermediate_plc["name"] + " applied " + str(self) + " because dep_val " + str(dep_val))
    def applyScadaDecision(self,generic_plc,action):
        """Apply the decision from the SCADA system
        
        Args:
            action: Can be 1.0 (open), 0.0 (closed), or a numeric value 0.0-2.0 for pump speed
        """
        if action == 1.0:
            action = "open"
        elif action == 0.0 or action == 0:
            action = "closed"
        # else: keep numeric value as-is for pump speed control (0.0-2.0)

        generic_plc.set_tag(self.actuator, action)


        return action
    def getScadaDecision(self,dependantValue,time):
        """Get a decision based on the control rule in the SCADA system and the dependant value """
        if dependantValue > self.value:
            return self.action
    def __str__(self):
        return "Control if {dependant} > {value} then set {actuator} to {action}".format(
            dependant=self.dependant, value=self.value, actuator=self.actuator, action=self.action)

    def applyHybridDecision(self, generic_plc, action, ScadaCommand):
        """Apply the decision from the Hybrid Control system
        
        Args:
            action: Can be 'rule', 'scada', 'open', 'closed', or a numeric value 0.0-2.0 for pump speed
        """
        if action == 'rule':
            self.apply(generic_plc)
        elif action == 'scada':
            self.applyScadaDecision(generic_plc, ScadaCommand)
        elif action == 'open':
            self.applyScadaDecision(generic_plc, 1.0)
        elif action == 'closed':
            self.applyScadaDecision(generic_plc, 0.0)
        else:
            # Numeric value for pump speed control (0.0-2.0)
            self.applyScadaDecision(generic_plc, action)


class TimeControl(Control):
    """
    Defines a TIME control, which takes no additional parameters.
    """

    def apply(self, generic_plc):
        """Applies the TIME control rule using a given PLC

        :param generic_plc: the PLC that will apply the control actions
        """
        curr_time = generic_plc.get_master_clock()
        if curr_time == self.value:
            generic_plc.set_tag(self.actuator, self.action)
            #generic_plc.logger.debug(
            #    generic_plc.intermediate_plc["name"] + " applied " + str(self) + " because curr_time " + str(curr_time))
    def applyScadaDecision(self,generic_plc,action):
        """Apply the decision from the SCADA system
        
        Args:
            action: Can be 1.0 (open), 0.0 (closed), or a numeric value 0.0-2.0 for pump speed
        """
        if action == 1.0:
            action = "open"
        elif action == 0.0 or action == 0:
            action = "closed"
        # else: keep numeric value as-is for pump speed control (0.0-2.0)

        generic_plc.set_tag(self.actuator, action)
    def getScadaDecision(self,dependantValue,time):
        curr_time=time
        if curr_time == self.value:
            return self.action

    def applyHybridDecision(self, generic_plc, action, ScadaCommand):
        """Apply the decision from the Hybrid Control system
        
        Args:
            action: Can be 'rule', 'scada', 'open', 'closed', or a numeric value 0.0-2.0 for pump speed
        """
        if action == 'rule':
            self.apply(generic_plc)
        elif action == 'scada':
            self.applyScadaDecision(generic_plc, ScadaCommand)
        elif action == 'open':
            self.applyScadaDecision(generic_plc, 1.0)
        elif action == 'closed':
            self.applyScadaDecision(generic_plc, 0.0)
        else:
            # Numeric value for pump speed control (0.0-2.0)
            self.applyScadaDecision(generic_plc, action)

    def __str__(self):
        return "Control if time = {value} then set {actuator} to {action}".format(
            value=self.value, actuator=self.actuator, action=self.action)
