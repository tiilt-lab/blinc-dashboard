import os as _rs_os, sys as _rs_sys  # noqa: E401
_rs_c = _rs_os.path.dirname(_rs_os.path.abspath(__file__))
while _rs_c != '/' and not _rs_os.path.isdir(_rs_os.path.join(_rs_c, 'common')):
    _rs_c = _rs_os.path.dirname(_rs_c)
_rs_c = _rs_os.path.join(_rs_c, 'common')
if _rs_c not in _rs_sys.path:
    _rs_sys.path.insert(0, _rs_c)
import reactor_safety  # reactor/thread boundary; src/common bootstrapped above
# Imported as video_cartoonizer.videoprocessor, so add this file's own dir for
# the bare sibling import below.
_rs_sys.path.insert(0, _rs_os.path.dirname(_rs_os.path.abspath(__file__)))
from frame_payload import build_frame_payload
from bounded_batch_queue import BoundedBatchQueue
import logging
import threading
import numpy as np
import cv2
import os
import torch
import torch.nn.functional as F
from .model.encoder.align_all_parallel import align_face
from .util import  get_video_crop_parameter,tensor2cv2
try:
    from moviepy.editor import *  # moviepy 1.x  # noqa: F403
except ModuleNotFoundError:
    from moviepy import *  # moviepy 2.x moved the public API to the package root  # noqa: F403
from scipy.io import wavfile
import traceback
from queue import Empty
# from .VideoMetricProcessor import VideoMetricAnalytics

WAIT_TIMEOUT = 0.05   # 50 ms (tune this)

class VideoProcessor:
    def __init__(self,cartoon_model,facial_emotion_detector,image_object_detection,attention_detection, \
                video_queue,frame_queue,cartoon_image_queue,config,cf,vid_img_dir,video_filename,aud_filename,sample_rate,depth,channels,video_interval):
        
        self.config = config
        self.cf = cf
        self.video_queue = video_queue
        self.frame_queue = frame_queue
        self.cartoon_image_queue = cartoon_image_queue
        self.vid_img_dir = vid_img_dir
        self.dat_filename = aud_filename + '.dat'
        self.wav_filename = aud_filename + '.wav'
        self.cart_vid = video_filename+'_caart'+'.mp4'
        self.depth = depth
        self.channels = channels
        self.sample_rate = sample_rate
        self.frame_count = 1
        self.running_processes = 0
        self.frame_array = []
        self.cartoon_model = cartoon_model
        self.facial_emotion_detector = facial_emotion_detector
        self.image_object_detection = image_object_detection
        self.attention_detection = attention_detection
        self.lock = threading.Lock()
        self.batch_size = cartoon_model.batch_size
        self.batch_track = 1
        self.first_valid_frame = True
        self.paras = None
        self.h = None
        self.w = None
        self.scale= None
        self.s_w = []
        self.frame_batch = []
        self.time_marker = []
        self.web_socket_connection = None
        self.facialEmbeddings = None
        self.video_interval = video_interval
        self.video_chunk_count = -1
        self.STOP = object()  # sentinel
        # One shared pool instead of creating threads per batch
        # self.pool = ThreadPoolExecutor(max_workers=3) #ProcessPoolExecutor(max_workers=4)
        self.pending_future = None
        




        if self.cartoon_model.video and self.cartoon_model.parsing_map_path is not None:
            self.x_p_hat = torch.tensor(np.load(self.cartoon_model.parsing_map_path))

        # self.VideoMetricAnalytics = VideoMetricAnalytics(self.attention_detection,self.facial_emotion_detector,self.image_object_detection)     

      
        self.non_cartoon_emotions = {"Extremely afraid":0,"Extremely alarmed":0,"Extremely annoyed":0,"Extremely aroused":0,"Extremely astonished":0,
                        "Extremely bored":0,"Extremely calm":0,"Extremely content":0,"Extremely delighted":0,"Extremely depressed":0,
                        "Extremely distressed":0,"Extremely droopy":0,"Extremely excited":0,"Extremely frustrated":0,"Extremely gloomy":0,
                        "Extremely happy":0,"Extremely miserable":0,"Extremely pleased":0,"Extremely sad":0,"Extremely satisfied":0,
                        "Extremely serene":0,"Extremely sleepy":0,"Extremely tensed":0,"Extremely tired":0,"Moderately afraid":0,
                        "Moderately alarmed":0,"Moderately annoyed":0,"Moderately aroused":0,"Moderately astonished":0,"Moderately bored":0,
                        "Moderately calm":0,"Moderately content":0,"Moderately delighted":0,"Moderately depressed":0,"Moderately distressed":0,
                        "Moderately droopy":0,"Moderately excited":0,"Moderately frustrated":0,"Moderately gloomy":0,"Moderately happy":0,"Moderately miserable":0,
                        "Moderately pleased":0,"Moderately sad":0,"Moderately satisfied":0,"Moderately serene":0,"Moderately sleepy":0,"Moderately tensed":0,
                        "Moderately tired":0," Neutral":0,"Slightly afraid":0,"Slightly alarmed":0,"Slightly annoyed":0,"Slightly aroused":0,"Slightly astonished":0,
                        "Slightly bored":0,"Slightly calm":0,"Slightly content":0,"Slightly delighted":0,"Slightly depressed":0,"Slightly distressed":0,
                        "Slightly droopy":0,"Slightly excited":0,"Slightly frustrated":0,"Slightly gloomy":0,"Slightly happy":0,"Slightly miserable":0,"Slightly pleased":0,
                        "Slightly sad":0,"Slightly satisfied":0,"Slightly serene":0,"Slightly sleepy":0,"Slightly tensed":0,"Slightly tired":0,"Very afraid":0,
                        "Very alarmed":0,"Very annoyed":0,"Very aroused":0,"Very astonished":0,"Very bored":0,"Very calm":0,"Very content":0,"Very delighted":0,
                        "Very depressed":0,"Very distressed":0,"Very droopy":0,"Very excited":0,"Very frustrated":0,"Very gloomy":0,"Very happy":0,"Very miserable":0,
                        "Very pleased":0,"Very sad":0,"Very satisfied":0,"Very serene":0,"Very sleepy":0,"Very tensed":0,"Very tired":0}
        self.cartoon_emotions = {"Extremely afraid":0,"Extremely alarmed":0,"Extremely annoyed":0,"Extremely aroused":0,"Extremely astonished":0,
                        "Extremely bored":0,"Extremely calm":0,"Extremely content":0,"Extremely delighted":0,"Extremely depressed":0,
                        "Extremely distressed":0,"Extremely droopy":0,"Extremely excited":0,"Extremely frustrated":0,"Extremely gloomy":0,
                        "Extremely happy":0,"Extremely miserable":0,"Extremely pleased":0,"Extremely sad":0,"Extremely satisfied":0,
                        "Extremely serene":0,"Extremely sleepy":0,"Extremely tensed":0,"Extremely tired":0,"Moderately afraid":0,
                        "Moderately alarmed":0,"Moderately annoyed":0,"Moderately aroused":0,"Moderately astonished":0,"Moderately bored":0,
                        "Moderately calm":0,"Moderately content":0,"Moderately delighted":0,"Moderately depressed":0,"Moderately distressed":0,
                        "Moderately droopy":0,"Moderately excited":0,"Moderately frustrated":0,"Moderately gloomy":0,"Moderately happy":0,"Moderately miserable":0,
                        "Moderately pleased":0,"Moderately sad":0,"Moderately satisfied":0,"Moderately serene":0,"Moderately sleepy":0,"Moderately tensed":0,
                        "Moderately tired":0," Neutral":0,"Slightly afraid":0,"Slightly alarmed":0,"Slightly annoyed":0,"Slightly aroused":0,"Slightly astonished":0,
                        "Slightly bored":0,"Slightly calm":0,"Slightly content":0,"Slightly delighted":0,"Slightly depressed":0,"Slightly distressed":0,
                        "Slightly droopy":0,"Slightly excited":0,"Slightly frustrated":0,"Slightly gloomy":0,"Slightly happy":0,"Slightly miserable":0,"Slightly pleased":0,
                        "Slightly sad":0,"Slightly satisfied":0,"Slightly serene":0,"Slightly sleepy":0,"Slightly tensed":0,"Slightly tired":0,"Very afraid":0,
                        "Very alarmed":0,"Very annoyed":0,"Very aroused":0,"Very astonished":0,"Very bored":0,"Very calm":0,"Very content":0,"Very delighted":0,
                        "Very depressed":0,"Very distressed":0,"Very droopy":0,"Very excited":0,"Very frustrated":0,"Very gloomy":0,"Very happy":0,"Very miserable":0,
                        "Very pleased":0,"Very sad":0,"Very satisfied":0,"Very serene":0,"Very sleepy":0,"Very tensed":0,"Very tired":0}
        
        logging.info('vid directory is {0}'.format(self.vid_img_dir))
        if not os.path.exists(self.vid_img_dir):
            os.mkdir(self.vid_img_dir)
        
        
    def start(self):
        self.running = True
        self.vid_pro_thread = threading.Thread(target=self.processing, name="vdeo-data-cartoonization")
        self.vid_pro_thread.daemon = True
        self.vid_pro_thread.start()
        

    def stop(self):
        self.running = False
        # Bounded join: this runs on the reactor thread at disconnect; an
        # unbounded join on a wedged worker froze ingest for every pod.
        thread = getattr(self, 'vid_pro_thread', None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
            if thread.is_alive():
                logging.warning('video processor thread did not stop within 10s; abandoning it (daemon)')

    def add_websocket_connection(self,web_socket):
        self.web_socket_connection = web_socket

    def setParticpantFacialEmbeddings(self,facialEmbeddings):
        self.facialEmbeddings = facialEmbeddings

    def send_json(self, message):
        # Called from the processing thread — via the reactor-safety boundary.
        reactor_safety.send_json(self.web_socket_connection, message)
        
    # def __complete_callback(self):
    #     try:
    #         self.convert_dat_to_wav()
    #         sortedfiles = [int(f.split(".")[0]) for f in os.listdir(self.vid_img_dir)]
    #         sortedfiles.sort()
    #         file_path = [os.path.join(self.vid_img_dir,str(fp)+".png") for fp in sortedfiles] 
    #         video = ImageSequenceClip(file_path,fps=29)
    #         audio_clip = AudioFileClip(self.wav_filename)
    #         new_audio_clip = CompositeAudioClip([audio_clip])
    #         video.audio = new_audio_clip
    #         video.write_videofile(self.cart_vid)

    #         for file in os.listdir(self.vid_img_dir):
    #             os.remove(os.path.join(self.vid_img_dir,file))
    #         os.rmdir(self.vid_img_dir)
    #         os.remove(self.wav_filename)
        
    #     except Exception as e:
    #         logging.info('Unable to build cartoonized frames into video or delete video file: {0}'.format(e))



    def convert_dat_to_wav(self):
        with open(self.dat_filename, "rb") as f:
            all_data = f.read()
            all_data = np.frombuffer(all_data, np.float32 if self.depth == 4 else np.int16, -1)
            chunk_length = int(len(all_data) / self.channels)
            all_data = np.reshape(all_data, (chunk_length, self.channels))
        wavfile.write(self.wav_filename, self.sample_rate, all_data)
        os.remove(self.dat_filename)


    def convert_image(self,frames,savetopath,width,height):
        device = "cpu" if self.cartoon_model.cpu else "cuda"
        kernel_1d = np.array([[0.125],[0.375],[0.375],[0.125]])
        frame_faces_accumulator = []
        faces_in_frame = {}
        processed_frame_track = []
        for frame_index, frame in enumerate(frames):
            # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if self.cartoon_model.scale_image:
                try:
                    if self.first_valid_frame:
                        self.paras,self.h,self.w,self.scale = get_video_crop_parameter(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY), self.cartoon_model.landmarkpredictor, self.cartoon_model.face_detector_model,self.cartoon_model.padding)
                        if self.paras is not None:
                            if self.scale <= 0.75:
                                frame = cv2.sepFilter2D(frame, -1, kernel_1d, kernel_1d)
                            if self.scale <= 0.375:
                                frame = cv2.sepFilter2D(frame, -1, kernel_1d, kernel_1d) 

                            resized_frame = cv2.resize(frame, (self.w, self.h),interpolation=cv2.INTER_AREA)

                            for index, para in enumerate(self.paras):
                                top,bottom,left,right,lm,face_lm = para
                                face = resized_frame[top:bottom, left:right] 
                                
                                with torch.no_grad():
                                    I = align_face(face,lm)
                                    I = self.cartoon_model.transform(I).unsqueeze(dim=0).to(device)
                                    inner_s_w = self.cartoon_model.pspencoder(I)
                                    inner_s_w = self.cartoon_model.vtoonify.zplus2wplus(inner_s_w)
                                    if self.cartoon_model.vtoonify.backbone == 'dualstylegan':
                                        if self.cartoon_model.color_transfer:
                                            inner_s_w = self.cartoon_model.exstyle
                                        else:
                                            inner_s_w[:,:7] = self.cartoon_model.exstyle[:,:7]

                                    self.s_w.append(inner_s_w)
                                    #append a particular face across frames in the key corresponding to the paras index
                                    if index in faces_in_frame:
                                        faces_in_frame[index]+= [self.cartoon_model.transform(face).unsqueeze(dim=0).to(device)]
                                    else:
                                        faces_in_frame[index] = [self.cartoon_model.transform(face).unsqueeze(dim=0).to(device)]

                            # frame_faces_accumulator.append(torch.stack(faces))
                            processed_frame_track.append(resized_frame)
                            self.first_valid_frame = False
                    #this means the first valid frame have been detected and paras has value        
                    elif  self.paras is not None:
                        if self.scale <= 0.75:
                            
                            frame = cv2.sepFilter2D(frame, -1, kernel_1d, kernel_1d)
                        if self.scale <= 0.375:
                            frame = cv2.sepFilter2D(frame, -1, kernel_1d, kernel_1d) 

                    resized_frame = cv2.resize(frame, (self.w, self.h),interpolation=cv2.INTER_AREA)
                    for index, para in enumerate(self.paras):
                        top,bottom,left,right,lm,face_lm = para
                        face = resized_frame[top:bottom, left:right] 

                        #append a particular face across frames in the key corresponding to the paras index
                        if index in faces_in_frame:
                            faces_in_frame[index]+= [self.cartoon_model.transform(face).unsqueeze(dim=0).to(device)]
                        else:
                            faces_in_frame[index] = [self.cartoon_model.transform(face).unsqueeze(dim=0).to(device)]
                        
                    processed_frame_track.append(resized_frame)
                        
                except Exception:
                    error_str = traceback.format_exc()
                    logging.info('exception occured while converting video to cartoon: {0}'.format(error_str))
                    continue  
                           
        cartoonized_faces = [] 
        for key, face_across_frame in faces_in_frame.items():   
            x = torch.cat(face_across_frame, dim=0)
            with torch.no_grad():
                if self.cartoon_model.video and self.cartoon_model.parsing_map_path is not None:
                    len_frames = len(frame_faces_accumulator)     
                    x_p = self.x_p_hat[len_frames+1-x.size(0):len_frames+1].to(device)
                else:
                    x_p = F.interpolate(self.cartoon_model.parsingpredictor(2*(F.interpolate(x, scale_factor=2, mode='bilinear', align_corners=False)))[0], 
                                    scale_factor=0.5, recompute_scale_factor=False).detach()
                # we give parsing maps lower weight (1/16)
                inputs = torch.cat((x, x_p/16.), dim=1)
                # d_s has no effect when backbone is toonify

                y_tilde = self.cartoon_model.vtoonify(inputs, self.s_w[key].repeat(inputs.size(0), 1, 1), d_s = self.cartoon_model.style_degree)        
            
                y_tilde = torch.clamp(y_tilde, -1, 1)
                cartoonized_faces.append(y_tilde)                    

        reversed_face_across_frame = zip(*cartoonized_faces)

        for j, faces_in_frame in enumerate(reversed_face_across_frame):
            for k, cart_face in enumerate(faces_in_frame):
                i_top = self.paras[k][0]
                i_bottom = self.paras[k][1]
                i_left = self.paras[k][2]
                i_right = self.paras[k][3]
                H =  i_bottom - i_top
                W =  i_right - i_left
                cartooned_image = tensor2cv2(cart_face.cpu())
                # logging.info('shape of cartoon image {0} {1}'.format(cartooned_image.shape,(W,H)))
                # save_to = os.path.join(self.vid_img_dir, "{0}_{1}.{2}".format(j,k,'png'))
                # cv2.imwrite(save_to,cartooned_image)
                resized_img = cv2.resize(cartooned_image, (W, H), interpolation = cv2.INTER_AREA)
                processed_frame_track[j][i_top:i_bottom, i_left:i_right,:] = resized_img

            if savetopath is None:
                success, encoded_frame =  cv2.imencode('.jpeg', processed_frame_track[j])
                if success:
                    payload = encoded_frame.tobytes() #base64.b64encode()
                    logging.info("i am about to send the cartoonized image")
                    # worker thread -> reactor-safety boundary (binary frame)
                    reactor_safety.send_message(
                        self.web_socket_connection, payload, is_binary=True)
                    logging.info("i have sent the cartoonized image")
                    # callbacks.post_cartoonized_image(self.config.auth_key,self.config.sessionId,self.config.deviceId, encoded_frame.tobytes())
                           
        
        if savetopath is not None:
            for i, processed_frame in enumerate(processed_frame_track):
                save_to = os.path.join(self.vid_img_dir, "{0}.{1}".format(i+1,'png'))
                cv2.imwrite(save_to,cv2.resize(processed_frame, (width,height), interpolation = cv2.INTER_AREA))

    def adjust_time(self,frame_sec_mark):
        offset = self.config.start_offset
        return round(offset+ (frame_sec_mark + (self.video_interval * self.video_chunk_count)))
    
    # (removed) process_video_analytics: dead — it referenced self.VideoMetricAnalytics,
    # which is never assigned, so any call would AttributeError. Its only
    # invocations were commented out. Live analytics go through the detect
    # worker via enqueue_latest_frame_payload / build_frame_payload below.

    def worker_process(self,frames_batch, time_markers, batch_idx):
        # Do cvtColor here if needed (OpenCV releases GIL, ok in threads)
        frames_batch = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames_batch]
        if self.config.videocartoonify and self.cf.video_cartoonize():
            logging.info('cartoonizing frame batch of size  {0}'.format(len(frames_batch)))
            self.convert_image(frames_batch, None, 500, 375)

        if self.cf.process_video_analytics():
            # logging.info('processing video analytics for batch {0} of size {1}'.format(batch_idx, len(frames_batch)))
            # 7th element is last_batch: the detect worker unpacks a 7-tuple
            # (matching videoprocessor_posthoc). Live sessions stream
            # indefinitely with no natural "final batch", so it is always
            # False here; teardown is driven by client disconnect, not this
            # flag. (Omitting it made every live batch raise ValueError:
            # not enough values to unpack, silently disabling live gaze/
            # attention analytics.)
            payload = build_frame_payload(frames_batch, self.facialEmbeddings, batch_idx, time_markers, self.vid_img_dir, self.config.auth_key, False)
            self.enqueue_latest_frame_payload(payload,self.config.auth_key)
            # self.process_video_analytics(frames_batch, self.facialEmbeddings, batch_idx, time_markers, self.vid_img_dir,self.config.auth_key)
            

    def enqueue_latest_frame_payload(self, payload, candidate_queue_id):
        """Hand a frame batch to the detect worker's per-pod queue: bounded to
        a couple of batches, drop-oldest, never blocks (audit B.3 — the old
        50-deep queue pinned ~12 GB per lagging 1080p pod)."""
        manager = self.image_object_detection.frame_queue_manager
        candidate_frame_queue = manager.get(candidate_queue_id)
        if candidate_frame_queue is None:
            candidate_frame_queue = manager[candidate_queue_id] = BoundedBatchQueue(candidate_queue_id)
        return candidate_frame_queue.put_latest(payload)


    def processing(self):
        while self.running:
            try:
                # block briefly to avoid CPU spin; adjust timeout for latency needs
                subclip_frames = self.video_queue.get(timeout=0.25)
            except Empty:
                continue

            if subclip_frames is self.STOP:
                break
            
            # logging.info("i just read from video queue for client {0}".format(self.config.auth_key))
            try:
                self.video_chunk_count+=1
                subclib_frame_count = 0
                for ts, frame in subclip_frames:
                    subclib_frame_count =  subclib_frame_count + 1
                    # defer expensive cvtColor unless strictly needed now
                    # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                    self.frame_batch.append(frame)
                    self.time_marker.append(self.adjust_time(ts))

                    if len(self.frame_batch) >= self.batch_size:
                        # if self.pending_future is not None and not self.pending_future.done():
                        #     try:
                        #         # Wait briefly for worker to finish
                        #         self.pending_future.result(timeout=WAIT_TIMEOUT)
                        #     except TimeoutError:
                        #         # Still not done after waiting → drop batch
                        #         self.frame_batch = []
                        #         self.time_marker = []

                        #         logging.debug("Dropped batch after waiting %.2f seconds for worker_process for %s",WAIT_TIMEOUT, self.config.auth_key)
                        #         continue

                        # hand off current lists; start fresh ones for the producer
                        frames = self.frame_batch
                        markers = self.time_marker
                        b_tracker = self.batch_track
                        self.frame_batch = []
                        self.time_marker = []

                        # submit to the pool
                        self.worker_process(frames, markers, b_tracker)
                        # self.pending_future = self.pool.submit(self.worker_process, frames, markers, b_tracker)
                        self.batch_track+=1

                    # we just need 290 frames of the 10 sec chunks, and break if the frames capture  is more than 290    
                    if  subclib_frame_count > 300:
                        break
            except Exception:
                error_str = traceback.format_exc()
                logging.warning('Exception thrown while extracting image subclib {0} {1}'.format(error_str, self.config.auth_key))
            finally:
                # The frame iterator owns a moviepy clip (ffmpeg reader
                # subprocess); the early break above abandons it, so close
                # explicitly or the reader leaks until process exit.
                if hasattr(subclip_frames, 'close'):
                    try:
                        subclip_frames.close()
                    except Exception:
                        pass

        # drain remaining frames
        if self.frame_batch:
            if self.pending_future is None or self.pending_future.done():
                frames = self.frame_batch
                markers = self.time_marker
                b_tracker = self.batch_track
                self.frame_batch = []
                self.time_marker = []
                self.worker_process(frames, markers, b_tracker)
                # self.pending_future  = self.pool.submit(self.worker_process, frames, markers, b_tracker)

        # self.pool.shutdown(wait=True)
        logging.info('frame cartoonization thread stopped for {0}.'.format(self.config.auth_key))

    # def processing(self):
    #     logging.info('frame cartoonization thread started for {0}.'.format(self.config.auth_key))
    #     frame_processing_thread = None
    #     while self.running or not self.video_queue.empty():
    #         try:
    #             subclip_frames = None if self.video_queue.empty() else self.video_queue.get(block=False)
            
    #             if subclip_frames is not None:
    #                 self.video_chunk_count+=1
    #                 subclib_frame_count = 0
    #                 for time_stamp, frame in subclip_frames:
    #                     subclib_frame_count =  subclib_frame_count + 1
    #                     # logging.info('tracking time stamp for frame {} in  batch : {} is  {}'.format(subclib_frame_count, self.batch_track, time_stamp))
    #                     # if frame_shape[1] > frame_shape[0]:
    #                     #     dim = (500,375)
    #                     # else:
    #                     #      dim = (375,500)   
    #                     # frame = cv2.resize(frame, (500,375),interpolation=cv2.INTER_AREA)
    #                     frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    #                     self.frame_array.append(frame)
    #                     self.frame_batch.append(frame)
    #                     self.time_marker.append(self.adjust_time(time_stamp))
    #                     if (len(self.frame_batch) == self.batch_size):
    #                         # logging.info('total appended per batch {0}'.format(len(self.frame_batch)))
    #                         frames_batch_copy = copy.deepcopy(self.frame_batch)

    #                         # logging.info('printing ocf detail attention: {} cartoonify: {} and {}'.format(self.cf.process_video_analytics(),self.cf.video_cartoonize(), self.config.videocartoonify))
    #                         if self.config.videocartoonify and self.cf.video_cartoonize():
    #                             self.convert_image(self.frame_batch,None,500,375)

                            
    #                         if self.cf.process_video_analytics():
    #                             #start attention tracking
    #                             video_processing_thread = threading.Thread(target=self.process_video_analytics, args=(frames_batch_copy,self.facialEmbeddings,self.batch_track,self.time_marker,self.vid_img_dir))
    #                             video_processing_thread.daemon = True
    #                             video_processing_thread.start()
                                
                                
                            
    #                         self.frame_batch = []
    #                         self.time_marker = []
    #                         self.batch_track+=1
                                

                                

    #                     self.frame_count = self.frame_count + 1  

    #                     # we just need 290 frames of the 10 sec chunks, and break if the frames capture  is more than 290    
    #                     if  subclib_frame_count > 300:
    #                         break

            
    #         except Exception as e:
    #             error_str = traceback.format_exc()
    #             logging.warning('Exception thrown while extracting image subclib {0} {1}'.format(error_str, self.config.auth_key))

    #     logging.info('frame cartoonization thread stopped for {0}.'.format(self.config.auth_key))

    #     # Processing after session has ended (post hoc processing)
    #     if self.config.videocartoonify and self.cf.video_cartoonize():
    #         if frame_processing_thread is not None:
    #             frame_processing_thread.join()
    #         # convert all image to cartoon and save
    #         Post_frame_batch = []
    #         post_frame_name = []
    #         for index,  s_frame in enumerate(self.frame_array):
    #             Post_frame_batch.append(s_frame)
    #             post_frame_name.append(os.path.join(self.vid_img_dir, "{0}.{1}".format(index+1,'png')))
    #             if (len(Post_frame_batch) == self.batch_size):
    #                 self.running_processes += 1
    #                 self.convert_image(Post_frame_batch,post_frame_name,500,375)
    #                 Post_frame_batch =  [] 
    #                 post_frame_name = []   
    #     # logging.info('i eventually called complete_callback')
    #     # self.__complete_callback() 
    #     # logging.info('frame cartoonization thread stopped for {0}.'.format(self.config.auth_key))
            

   
                                
