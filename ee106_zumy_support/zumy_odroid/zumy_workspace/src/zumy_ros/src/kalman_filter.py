#!/usr/bin/env python
import rospy
import numpy as np
from numpy.linalg import inv
import sys
from geometry_msgs.msg import Transform,TransformStamped
from zumy_ros.srv import ImuSrv,ImuSrvResponse,NuSrv,NuSrvResponse
from tf2_msgs.msg import TFMessage
from std_msgs.msg import String,Header,Int32,Float32,Bool

class KalmanFilter:
  def __init__(self,mname):

    #Initialize the node
    rospy.init_node('kalman_filter_' + mname)
    self.hertz = 10
    self.dt = 1./self.hertz
    self.rate = rospy.Rate(self.hertz)
    self.calTime = 5 # Calibration time in seconds
    self.initial_position_uncertainty = 0.10 # meters
    self.initial_orientation_uncertainty = 0.3 # radians (0.3rad ~ 15deg)
    self.camera_position_error = 0.05 # meters
    self.camera_orientation_error = 0.1 # radians (0.1rad ~ 5deg)
    self.C_lin = np.array([[1.,0.,0.,0.],[0.,0.,1.,0]]) # position camera measurement
    self.C_ang = 1. # orientation camera measurement
    self.G_lin = np.array([[0.5*pow(self.dt,2),self.dt,0.,0.],
                          [0.,0.,0.5*pow(self.dt,2),self.dt]]).T # position IMU input
    self.G_ang = self.dt # orientation IMU input
    self.A_lin = np.array([[1.,self.dt,0.,0.],[0.,1.,0.,0.],[0.,0.,1.,self.dt],[0.,0.,0.,1.]]) # translation dynamics
    self.A_ang = 1. # rotation dynamics
    self.updateFlag = True
    self.mname = mname

    # Measurement initialization -- only necessary temporarily while AR does not work
    self.u = None
    self.z = Transform()
    self.origin_tag = 'usb_cam'
   
    self.needs_to_calibrate = False
 
    #Create a publisher to the state estimation topic
    # self.done_calibration_pub = rospy.Publisher('/' + mname + '/done_calibration', Bool, queue_size=2)
    self.state_pub = rospy.Publisher('/' + mname + '/state_estimate', Transform,queue_size=2)
    self.state_pub_tf = rospy.Publisher('/tf', TFMessage,queue_size=2)
    self.psi_pub = rospy.Publisher("/" + mname + "/psi", Float32, queue_size=2)

    self.calibrate_sub = rospy.Subscriber("/"+ mname +"/calibrate", Float32, self.calibrateCallback)

    #Create the service for the AR tag client
    rospy.Service('innovation', NuSrv, self.triggerUpdate)

  def calibrateCallback(self, message):
    self.needs_to_calibrate = True

  # Compute the time update for every time step based on measured variations
  def timeUpdate(self,u):
    # Define inputs
    u_lin = np.array([u.linear_acceleration_filtered.x,u.linear_acceleration_filtered.y]) - self.acc_bias
    u_ang = u.angular_velocity_filtered.z - self.gyro_bias
    # Determine orientation
    rot = np.array([[np.cos(self.psi),-np.sin(self.psi)],[np.sin(self.psi),np.cos(self.psi)]])
    rotxv = np.kron(rot,np.eye(2))
    # Propagate dynamics
    self.x_lin += self.v_lin*self.dt + 0.5*rot.dot(u_lin)*pow(self.dt,2)
    self.v_lin += rot.dot(u_lin)*self.dt
    self.psi += u_ang*self.dt
    # Update uncertainty
    self.P_lin = self.A_lin.dot(self.P_lin).dot(self.A_lin.T)  \
                 + rotxv.dot(self.Q_lin).dot(rotxv.T)
    self.P_ang += self.Q_ang
 

  # Compute a measurement update based on the received information
  def measurementUpdate(self,z):
    # Compute innovation
    e_lin = np.array([z.translation.x, z.translation.y]) - self.x_lin
    # Convert angular states to (-2pi, +2pi)
    e_ang = 2. * np.arccos(z.rotation.w) * np.sign(z.rotation.z) \
            - (np.mod(self.psi, np.sign(self.psi)*2*np.pi))
    S_lin  = self.C_lin.dot(self.P_lin).dot(self.C_lin.T) + self.R_lin
    S_ang  = self.C_ang*self.P_ang*self.C_ang + self.R_ang
    # Compute Kalman gain
    K_lin = self.P_lin.dot(self.C_lin.T).dot(inv(S_lin))
    K_ang = self.P_ang*self.C_ang/S_ang
    # Update state
    xv = np.array([self.x_lin[0],self.v_lin[0],self.x_lin[1],self.v_lin[1]])
    xv += K_lin.dot(e_lin)
    self.x_lin = np.array([xv[0],xv[2]])
    self.v_lin = np.array([xv[1],xv[3]]) # np.array([0,0]) # 
    self.psi += K_ang*e_ang
    # Update uncertainty
    self.P_lin = (np.eye(4)-K_lin.dot(self.C_lin)).dot(self.P_lin)
    # Reset flag
    self.updateFlag = False


  # When another node calls the service sending a measurement, incorporate it
  def triggerUpdate(self,request):
    print "updating..."
    self.z = request.transform
    self.z_position = request.transform.translation.z
    self.origin_tag = request.origin_tag
    # We will ignore the time stamp and assume that the AR fix is from the latest time step
    self.updateFlag = True
    return []

  # Calibrate sensors to determine bias and variance
  def calibrateSensors(self):
    startCal = rospy.get_rostime() # node time in seconds
    print "Starting sensor calibration..."
    m = np.empty([6,int(self.hertz*self.calTime)])
    j = 0
    while j < m.shape[1]:
        rospy.wait_for_service('last_imu')
        try:
            get_imu = rospy.ServiceProxy('last_imu', ImuSrv)
            u = get_imu()
            m[:,j] = [u.linear_acceleration_filtered.x,u.linear_acceleration_filtered.y,u.linear_acceleration_filtered.z,
                      u.angular_velocity_filtered.x, u.angular_velocity_filtered.y, u.angular_velocity_filtered.z]
            j = j + 1
        except rospy.ServiceException, e:
            #print "Service call to IMU Server failed: %s"%e
            print "No IMU update this time step: " + str(j) + ", trying again."
            m[:,j] = np.zeros(6)
        self.rate.sleep()
    endCal = rospy.get_rostime()
    print "Calibration complete. Took %f seconds"%(endCal-startCal).to_sec()
    Q = np.cov(m,bias=1)
    mu = np.mean(m,axis=1)
    print "Average accelerometer measurement: [%f, %f, %f]"%(mu[0], mu[1], mu[2])
    print "Accelerometer covariance matrix:"
    print Q[:3,:3]
    print "Average gyroscope measurement: [%f, %f, %f]"%(mu[3], mu[4], mu[5])
    print "Gyroscope covariance matrix:"
    print Q[3:,3:]
    return (Q,mu)

  # Update accelerometer bias estimates (assumes the zumy is generally near zero acceleration)
  # (this function serves as a low-pass filter to estimate gravity and accelerometer errors)
  def updateAccelBias(self):
    alpha = 0.8 # between 0 and 1 (set to 0 to ignore accelerometer measurements)
    bias_delta = np.array([self.u.linear_acceleration_filtered.x,
                           self.u.linear_acceleration_filtered.y])
    self.acc_bias = (alpha) * self.acc_bias + (1-alpha) * bias_delta
    print 'bias:\n' + str(self.acc_bias)

  # Main node execution function
  def run(self):

    counter = 0;

    # set default Q and mu values to eye(6) and zero
    Q = np.eye(6)
    mu = np.zeros(6)

    # Initial sensor calibration
    Q, mu = self.calibrateSensors()

    # Initialize Kalman filter
    self.P_lin = np.diag([1.,0.,1.,0.])*pow(self.initial_position_uncertainty,2)
    self.Q_lin = self.G_lin.dot(Q[:2,:2]).dot(self.G_lin.T)
    self.P_ang = pow(self.initial_orientation_uncertainty,2)
    self.Q_ang = self.G_ang*Q[5,5]*self.G_ang
    self.R_lin = np.eye(2)*pow(self.camera_position_error,2)
    self.R_ang = pow(self.camera_orientation_error,2)
    self.acc_bias = mu[:2]
    self.gyro_bias = mu[5]
    self.x_lin = np.array([self.z.translation.x, self.z.translation.y])
    self.z_position = 0.
    self.v_lin = np.array([0.,0.])
    self.psi = 2*np.arccos(self.z.rotation.w)*np.sign(self.z.rotation.z) # Assume we have a quaternion with vertical axis

    # Run Kalman filter
    while not rospy.is_shutdown() and not self.needs_to_calibrate:
      # Obtain IMU measurement
      rospy.wait_for_service('last_imu')
      try:
        get_imu = rospy.ServiceProxy('last_imu', ImuSrv)
        self.u = get_imu()
        # update bias estimate for accelerometer IMU update this time step: 1, trying aga
        self.updateAccelBias()
      except rospy.ServiceException, e:
        #print "Service call to IMU Server failed: %s"%e
        print "No IMU update this time step"
        # Only runs if self.u is unitialized
        while not self.u:
            try:
                self.u = get_imu()
            except:
                print 'Trying to initialize'
        # Assume previous measured u (Zero-Order Hold)

      # Perform time and measurement updates as appropriate
      self.timeUpdate(self.u)
      if self.updateFlag:
        self.measurementUpdate(self.z)

      # Publish state estimate in topic
      state = Transform()
      state.translation.x = self.x_lin[0]
      state.translation.y = self.x_lin[1]
      state.translation.z = self.z_position
      state.rotation.z = np.sin(self.psi/2)
      state.rotation.w = np.cos(self.psi/2) # Quaternion form
      self.state_pub.publish(state)
      self.psi_pub.publish(self.psi)

      state_tf = TFMessage()
      state_tf.transforms = [TransformStamped()]
      state_tf.transforms[0].header.seq = counter
      state_tf.transforms[0].header.frame_id = self.origin_tag
      state_tf.transforms[0].child_frame_id = self.mname
      state_tf.transforms[0].transform = state
      self.state_pub_tf.publish(state_tf)

      counter = counter + 1
      # Finish cycle and loop
      self.rate.sleep()
      
    self.needs_to_calibrate = False

#Python's syntax for a main() method
if __name__ == '__main__':
    mname = sys.argv[1]
    node = KalmanFilter(mname)
    while True:    
      node.run()
